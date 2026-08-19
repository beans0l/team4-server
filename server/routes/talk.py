"""WS /doll/talk — 아이와 인형의 실시간 음성 대화.

    Android  ──WS──>  이 서버  ──WS──>  Gemini Live
             <──WS──          <──WS──

서버가 중계하는 이유는 하나다: **API 키는 서버에만 있어야 한다.** 앱이 Live 를 직접
부르면 APK 를 뜯어서 키를 꺼낼 수 있고, 크레딧이 $10 뿐이라 유출되면 시연 전에 소진된다.

연결 URL (앱과의 연동 계약):

    /doll/talk?token=<로그인 JWT>&child=지우&age=4&interests=공룡,딸기&doll=초록이

    🔴 token 은 필수다 — 없거나 유효하지 않으면 즉시 거절한다(아래 실패 표).
    핸드셰이크에 커스텀 헤더를 못 싣는 WS 클라이언트도 있어서, HTTP 라우트들처럼
    Authorization 헤더가 아니라 다른 필드와 같은 방식(쿼리파라미터)으로 받는다.

    child/age/interests/doll 은 전부 선택값이다. 없으면 기본 페르소나('초록이', 4살)로
    돈다 — 회원가입에 이름·나이 필드가 아직 없고 관심사는 나중에 입력하는 값이라, 빈
    채로 오는 게 정상 경로다. 프로필이 없다고 대화를 거절하면 안 된다.

    아이 정보를 **연결할 때 앱이 실어 보낸다.** 서버가 회원 DB 를 조회하지 않는
    이유는 server/profile.py 참조 — token 으로 얻는 user_id 는 대화 기록을
    저장할 때만 쓰고(아래), 페르소나 구성에는 안 쓴다.

    세션이 끝나면(정상 종료·오류 모두) user_id·인형이 한 말(턴 단위)·시작/종료 시각을
    talk_sessions 테이블에 기록한다. 아이 쪽 음성은 텍스트로 전사되지 않으므로
    저장되는 대화 내용은 인형 발화뿐이다.

프레임 규약 (앱과의 연동 계약 — 바꾸려면 앱 파트와 합의):

    업   binary  16kHz / 16-bit / mono PCM, 100ms(3200바이트) 청크
         text    {"type":"activity_start"}   아이가 말하기 시작
         text    {"type":"activity_end"}     아이가 말을 끝냄

    다운 binary  24kHz PCM — 그대로 재생하고 RMS 로 립싱크
         text    {"type":"transcript","text":"..."}
         text    {"type":"turn_complete"}
         text    {"type":"session_reset"}    재연결됨, 이전 맥락 없음
         text    {"type":"error","code":"...","message":"..."}

⚠️ activity_start / activity_end 를 **앱이 판단해서 보낸다.** 서버가 대신 정해줄 수
   없다 — 마이크를 가진 쪽이 앱이기 때문이다. 실측에서 `audio_stream_end` 는 응답이
   아예 안 왔고 자동 VAD 는 느렸다(18.7초). 수동 신호가 유일하게 동작한 조합이다.
   식사 중에는 식기 소리가 섞이므로 데모에서는 푸시투토크 버튼이 안전하다(R6).
"""

import asyncio
import json
import logging
from datetime import datetime

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from server import config, db
from server.deps import get_user_id_from_token
from server.errors import LiveUnavailableError, PipelineError, TalkBusyError, UnauthorizedError
from server.live import LiveSession
from server.profile import ChildProfile
from server.prompts import render_persona

log = logging.getLogger(__name__)

router = APIRouter(tags=["doll"])

# 동시 대화 수 제한. stylize 의 세마포어(MAX_CONCURRENCY)와 **별개**다 —
# 저쪽은 메모리(요청당 1GB)가 병목이고, 이쪽은 크레딧이 병목이다.
_slots = asyncio.Semaphore(config.MAX_TALK_SESSIONS)

# WS close code
_CLOSE_NORMAL = 1000
_CLOSE_INTERNAL = 1011
_CLOSE_TRY_LATER = 1013
_CLOSE_UNAUTHORIZED = 1008  # policy violation


def _save_talk_session(
    user_id: int, transcript: list[str], started_at: datetime, ended_at: datetime
) -> None:
    with db.get_cursor() as cur:
        cur.execute(
            "INSERT INTO talk_sessions (user_id, transcript, started_at, ended_at) "
            "VALUES (%s, %s, %s, %s)",
            (user_id, json.dumps(transcript, ensure_ascii=False), started_at, ended_at),
        )


async def _send_error(ws: WebSocket, exc: PipelineError) -> None:
    """WS 는 HTTP 예외 핸들러를 타지 않는다(main.py 참조). 여기서 직접 변환한다."""
    try:
        await ws.send_json(
            {"type": "error", "code": exc.code, "message": exc.message}
        )
    except Exception:
        pass  # 앱이 이미 끊었으면 보낼 곳이 없다. 정상이다.


async def _close(ws: WebSocket, code: int) -> None:
    """이미 닫힌 연결에 close 를 부르면 RuntimeError 가 난다.

    그게 finally 안에서 터지면 진짜 원인(예: LIVE_UNAVAILABLE)을 덮어쓴 채
    "처리되지 않은 오류" 스택트레이스만 남는다.
    """
    try:
        await ws.close(code=code)
    except Exception as e:
        log.debug("close 무시됨 (이미 닫힘): %s", e)


async def _uplink(ws: WebSocket, session: LiveSession) -> None:
    """앱 -> Gemini. 앱이 끊으면 조용히 끝난다."""
    while True:
        msg = await ws.receive()

        if msg["type"] == "websocket.disconnect":
            log.info("앱이 연결을 끊음 (code=%s)", msg.get("code"))
            return

        pcm = msg.get("bytes")
        if pcm is not None:
            await session.send_audio(pcm)
            continue

        text = msg.get("text")
        if text is None:
            continue

        # 잘못된 제어 프레임 하나로 대화를 끊지 않는다. 아이는 그동안 말하는 중이다.
        try:
            kind = json.loads(text).get("type")
        except (json.JSONDecodeError, AttributeError):
            log.warning("제어 프레임 파싱 실패, 무시: %.80s", text)
            continue

        if kind == "activity_start":
            await session.send_activity_start()
        elif kind == "activity_end":
            await session.send_activity_end()
        else:
            log.warning("알 수 없는 제어 프레임 무시: %s", kind)


async def _downlink(ws: WebSocket, session: LiveSession, transcript: list[str]) -> None:
    """Gemini -> 앱.

    transcript 프레임은 한 턴을 여러 조각으로 나눠 보낸다(README 참조). turn_complete
    가 와야 한 턴이 끝난 것이므로, 그때 조각을 이어붙여 transcript(DB 저장용)에 쌓는다.
    """
    turn_buf: list[str] = []
    async for kind, payload in session.events():
        try:
            if kind == "audio":
                await ws.send_bytes(payload)
            elif kind == "transcript":
                turn_buf.append(payload)
                await ws.send_json({"type": "transcript", "text": payload})
            else:
                # turn_complete / session_reset
                if kind == "turn_complete" and turn_buf:
                    transcript.append("".join(turn_buf))
                    turn_buf.clear()
                await ws.send_json({"type": kind})
        except (WebSocketDisconnect, RuntimeError) as e:
            # 앱이 먼저 끊었다. 보통은 _uplink 가 먼저 알아채지만, 인형이 말하는
            # 도중에 끊기면 이쪽이 먼저 걸린다. 정상 종료다 — 스택트레이스를
            # 남기면 진짜 오류와 구분이 안 된다.
            if turn_buf:
                transcript.append("".join(turn_buf))  # 미완성 턴이라도 버리지 않는다
            log.info("다운링크 종료 — 앱 연결이 이미 닫힘: %s", e)
            return


@router.websocket("/doll/talk")
async def talk(ws: WebSocket):
    # 인증을 가장 먼저 본다 — 로그인 안 한 요청에는 API 키 유무나 세션 혼잡도 같은
    # 서버 상태를 알려줄 이유가 없고, 뒤에 오는 Live 연결·세마포어 점유도 아낀다.
    # 토큰은 헤더가 아니라 쿼리파라미터로 받는다: 핸드셰이크에 커스텀 헤더를 못
    # 싣는 WS 클라이언트가 있어서 나머지(child/age/...)와 같은 방식으로 통일했다.
    try:
        user_id = get_user_id_from_token(ws.query_params.get("token"))
    except UnauthorizedError as e:
        await ws.accept()
        await _send_error(ws, e)
        await _close(ws, _CLOSE_UNAUTHORIZED)
        log.warning("대화 요청 거절 — 인증 실패: %s", e.detail)
        return

    # 키 확인은 accept 전에. 없으면 Live 연결이 100% 실패하므로 붙기 전에 거절한다.
    # 다만 **accept 는 해야 error 프레임을 보낼 수 있다** — accept 하지 않고 닫으면
    # 앱에는 HTTP 403 만 보이고 code 를 못 읽는다.
    if not config.GEMINI_API_KEY:
        await ws.accept()
        await _send_error(
            ws,
            LiveUnavailableError("GEMINI_API_KEY 미설정 (서버의 .env 확인)"),
        )
        await _close(ws, _CLOSE_INTERNAL)
        log.error("대화 요청 거절 — GEMINI_API_KEY 없음")
        return

    # locked() 확인과 acquire() 사이에는 await 가 없다. 이벤트 루프는 단일 스레드라
    # 그 사이에 다른 연결이 끼어들 수 없으므로 이 검사는 안전하다.
    if _slots.locked():
        await ws.accept()
        await _send_error(ws, TalkBusyError(f"동시 {config.MAX_TALK_SESSIONS}건 초과"))
        await _close(ws, _CLOSE_TRY_LATER)
        log.warning("대화 요청 거절 — 동시 세션 한도(%d)", config.MAX_TALK_SESSIONS)
        return
    await _slots.acquire()

    # 🔐 프로필에는 아이 이름이 들어 있다. 로그에는 safe_repr() 만 남긴다 —
    #    서버 로그는 팀 전체가 보고 시연 중 화면에 띄우기도 한다.
    profile = ChildProfile.from_query(ws.query_params)

    await ws.accept()
    log.info(
        "대화 시작 — model=%s voice=%s %s",
        config.LIVE_MODEL,
        config.DOLL_VOICE,
        profile.safe_repr(),
    )

    session = LiveSession(persona=render_persona(profile))
    transcript: list[str] = []
    started_at = datetime.now()
    try:
        # 🔴 순서가 중요하다. 다운링크가 Live 연결을 여는 주체이므로 **먼저** 띄우고,
        #    연결이 설 때까지 기다린 뒤에 업링크를 시작한다.
        #
        #    반대로 하면(둘을 동시에 시작하면) 연결이 서기 전 약 0.5초 동안 앱이 보낸
        #    프레임을 읽어서 버리게 된다. 그중에 activity_start 가 있으면 Gemini 는
        #    발화가 시작된 줄 모른 채 activity_end 만 받고, **아무 응답도 하지 않는다.**
        #    실제로 이렇게 만들었다가 35초 무응답을 봤다. 서버 로그의 유일한 단서는
        #    `dropped=5` 한 줄이었다.
        #
        #    이 동안 앱이 보낸 프레임은 소켓 버퍼에 쌓였다가 순서대로 읽힌다 —
        #    읽는 시점만 늦출 뿐 잃지 않는다.
        down = asyncio.create_task(_downlink(ws, session, transcript), name="talk-downlink")
        ready = asyncio.create_task(
            session.wait_ready(config.LIVE_CONNECT_TIMEOUT_SEC), name="talk-ready"
        )
        first, _ = await asyncio.wait(
            {down, ready}, return_when=asyncio.FIRST_COMPLETED
        )

        if down in first:
            # 연결도 못 하고 다운링크가 끝났다. 안에서 난 예외를 그대로 올린다.
            ready.cancel()
            down.result()
            raise LiveUnavailableError("연결되기 전에 다운링크가 종료됨")

        if not ready.result():
            down.cancel()
            raise LiveUnavailableError(
                f"첫 연결이 {config.LIVE_CONNECT_TIMEOUT_SEC}초 안에 서지 않음"
            )

        # 한쪽이 끝나면(앱이 끊었거나 Live 가 죽었거나) 다른 쪽도 취소해야 한다 —
        # 안 그러면 앱이 나간 뒤에도 Live 연결이 살아서 크레딧을 계속 먹는다.
        tasks = [asyncio.create_task(_uplink(ws, session), name="talk-uplink"), down]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

        # 먼저 끝난 쪽이 예외였다면 여기서 다시 올라온다.
        for t in done:
            t.result()

    except WebSocketDisconnect:
        log.info("대화 종료 — 앱이 연결을 끊음")
    except PipelineError as e:
        log.warning("대화 실패: %s — %s", e.code, e.detail)
        await _send_error(ws, e)
        await _close(ws, _CLOSE_INTERNAL)
    except Exception as e:
        log.exception("대화 중 처리되지 않은 오류")
        await _send_error(ws, PipelineError(str(e)))
        await _close(ws, _CLOSE_INTERNAL)
    else:
        await _close(ws, _CLOSE_NORMAL)
    finally:
        # 🔴 순서가 중요하다. Live 를 먼저 닫아야 크레딧이 멎는다.
        await session.close()
        try:
            await asyncio.to_thread(
                _save_talk_session, user_id, transcript, started_at, datetime.now()
            )
        except Exception:
            # 대화 자체는 이미 끝났다. 기록 실패로 앱에 에러를 보여줄 이유는 없다.
            log.exception("대화 기록 저장 실패 — user_id=%d", user_id)
        _slots.release()
