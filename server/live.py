"""Gemini Live 세션 래퍼 — 음성↔음성 대화.

동작하는 레퍼런스는 `ai/dialog_test.py` 의 `path_c_async()` 다(과제 4, TTFB 0.85초).
여기서는 거기서 확인한 설정을 그대로 쓰되, **한 번 쏘고 끝나는 스크립트**를
**18분짜리 세션**으로 바꾸면서 필요해진 두 가지를 더한다: 재연결과 사용량 계측.

    routes/talk.py  ─send_audio()─>  이 파일  ─>  Gemini Live
                    <──events()───

실측으로 걸린 함정 (바꾸기 전에 반드시 읽을 것):

  ① `audio_stream_end=True` 는 동작하지 않는다. 응답이 오지 않고 멎는다(35초 타임아웃).
     그래서 자동 VAD 를 끄고(`AutomaticActivityDetection(disabled=True)`)
     앱이 보내는 activity_start / activity_end 를 그대로 중계한다.
     자동 VAD 를 켜는 대안도 되긴 하지만 응답 완료가 18.7초로 느려진다.

  ② `system_instruction` 이 없으면 페르소나가 통째로 무시된다. 반말 친구가 아니라
     `저는 유치원에 다니지 않아서...` 하는 존댓말 어시스턴트가 나온다.

  ③ `output_audio_transcription` 은 립싱크·로깅에 필요하다. 인형이 실제로 뭐라고
     말했는지 알 방법이 이것뿐이다(오디오만 오면 서버는 내용을 모른다).
"""

import asyncio
import logging
import time
from contextlib import AsyncExitStack

from ai.tts_test import _gemini_client
from server import config
from server.errors import LiveUnavailableError
from server.prompts import render_persona

log = logging.getLogger(__name__)

# 재연결 백오프. 짧게 잡는다 — 아이가 인형 앞에 앉아 기다리는 중이라
# 지수 백오프를 길게 끌면 대화가 끊긴 것처럼 느껴진다.
_BACKOFF_SEC = (0.5, 1.0, 2.0)


def _backoff(attempt: int) -> float:
    return _BACKOFF_SEC[min(attempt, len(_BACKOFF_SEC)) - 1]


def _live_config(persona: str):
    """LiveConnectConfig. path_c_async() 와 같은 값이어야 실측 지연이 재현된다.

    persona 는 세션마다 다르다(아이 이름·나이·관심사가 들어 있다). 모듈 상수로
    두면 안 된다 — 서버 기동 시 한 번 읽힌 값이 모든 아이에게 쓰인다.
    """
    from google.genai import types

    return types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(
                    voice_name=config.DOLL_VOICE
                )
            )
        ),
        # ② 빠뜨리면 페르소나가 무시된다. 문자열이 아니라 Content 로 넘겨야 한다.
        system_instruction=types.Content(parts=[types.Part(text=persona)]),
        # ③ 립싱크·로깅용. 인형이 한 말을 텍스트로도 받는다.
        output_audio_transcription=types.AudioTranscriptionConfig(),
        # ① 자동 VAD 를 끄고 앱의 수동 신호를 쓴다.
        realtime_input_config=types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(disabled=True)
        ),
    )


class Usage:
    """R5(Live 비용 미측정) 를 해소하기 위한 계측.

    🔴 나중에 붙이면 시연 전까지 못 잰다. 그래서 처음부터 넣는다.
       밥 한 끼(18분) 동안 연결을 켜두는 서비스라 분당 단가가 기획을 흔들 수 있다.

    ⚠️ usage_metadata 가 **누적값인지 이번 응답분(델타)인지 확정하지 못했다.**
       둘을 잘못 알면 비용이 수십 배로 틀린다. 그래서 합계와 최댓값을 **둘 다**
       남기고, 로그를 본 사람이 판단하게 한다:
         - 델타라면  total_sum   이 맞는 값이고 last_max 는 마지막 한 턴이다
         - 누적이라면 last_max   가 맞는 값이고 total_sum 은 크게 부풀려진다
       두 값이 비슷하면 턴이 한 번뿐이었다는 뜻이라 판정할 수 없다. 여러 턴을
       주고받은 세션(ai/talk_client.py --soak)에서 봐야 갈린다.
    """

    def __init__(self):
        self.started = time.perf_counter()
        self.total_sum = 0
        self.last_max = 0
        self.turns = 0
        self.by_modality: dict[str, int] = {}

    def add(self, meta) -> None:
        if meta is None:
            return
        total = getattr(meta, "total_token_count", None) or 0
        if total:
            self.total_sum += total
            self.last_max = max(self.last_max, total)

        # modality 별 분해가 있으면 같이 모은다. 오디오 토큰이 대부분일 것으로
        # 예상되는데, 실제로 그런지는 이 값으로만 확인할 수 있다.
        for attr in ("prompt_tokens_details", "response_tokens_details"):
            for d in getattr(meta, attr, None) or []:
                name = str(getattr(d, "modality", "?")).split(".")[-1].lower()
                self.by_modality[name] = self.by_modality.get(name, 0) + (
                    getattr(d, "token_count", None) or 0
                )

    def summary(self) -> dict:
        elapsed = max(time.perf_counter() - self.started, 1e-6)
        minutes = elapsed / 60
        return {
            "seconds": round(elapsed, 1),
            "turns": self.turns,
            "total_sum": self.total_sum,
            "last_max": self.last_max,
            # 이 두 값의 차이가 위 ⚠️ 의 판정 재료다.
            "tokens_per_min_if_delta": round(self.total_sum / minutes),
            "tokens_per_min_if_cumulative": round(self.last_max / minutes),
            "by_modality": self.by_modality,
        }


class LiveSession:
    """Live 연결 하나. 끊기면 스스로 다시 붙는다.

    사용법 — 보내는 쪽과 받는 쪽을 별도 태스크로 돌린다:

        session = LiveSession()
        async for evt in session.events():   # 받는 쪽이 재연결을 소유한다
            ...
        await session.send_audio(pcm)        # 보내는 쪽

    **재연결의 주체가 받는 쪽인 이유**: 연결이 끊겼다는 사실을 가장 먼저 아는 게
    receive() 다. 보내는 쪽에서도 재연결하면 두 태스크가 동시에 새 연결을 만들어
    세션이 두 개가 된다(그리고 둘 다 과금된다).
    """

    def __init__(self, persona: str | None = None):
        self._client = _gemini_client()
        # 🔴 인스턴스에 보관해야 한다. _open() 은 재연결마다 다시 불리는데, 지역변수로
        #    두면 **재연결 후 인형이 아이 이름을 잊는다.** session_reset 으로 대화
        #    맥락이 날아가는 건 앱도 아는 정상 동작이지만, 이름까지 잃으면 아이
        #    입장에서는 인형이 딴 사람이 된 것처럼 보인다.
        self._persona = persona or render_persona()
        self._stack: AsyncExitStack | None = None
        self._session = None
        # 연결이 살아 있는 동안만 set. 재연결 중에 들어온 오디오는 여기서 걸러 버린다.
        self._ready = asyncio.Event()
        self.usage = Usage()
        self.dropped_chunks = 0

    # --- 연결 -------------------------------------------------------------
    async def _open(self) -> None:
        stack = AsyncExitStack()
        self._session = await stack.enter_async_context(
            self._client.aio.live.connect(
                model=config.LIVE_MODEL, config=_live_config(self._persona)
            )
        )
        self._stack = stack
        self._ready.set()

    async def _shutdown(self) -> None:
        """현재 연결만 정리한다. 실패해도 무시 — 어차피 버릴 연결이다."""
        self._ready.clear()
        self._session = None
        stack, self._stack = self._stack, None
        if stack is not None:
            try:
                await stack.aclose()
            except Exception as e:
                log.debug("Live 연결 정리 중 무시된 오류: %s", e)

    async def close(self) -> None:
        await self._shutdown()
        log.info("Live 세션 종료 — usage=%s dropped=%d", self.usage.summary(), self.dropped_chunks)

    async def wait_ready(self, timeout: float) -> bool:
        """연결이 설 때까지 기다린다. 시간 안에 못 서면 False.

        연결은 즉시 되지 않는다(실측 약 0.5초). 그동안 앱이 보낸 것을 그냥 버리면
        **activity_start 가 사라지고, 그러면 Gemini 는 발화가 시작된 줄 모른 채
        activity_end 만 받아 아무 응답도 하지 않는다.** 증상은 "35초 타임아웃"이라
        원인이 연결 타이밍이라는 걸 알아채기 어렵다. 실제로 한 번 겪었다.
        """
        try:
            await asyncio.wait_for(self._ready.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # --- 업링크 -----------------------------------------------------------
    async def _send(self, wait_sec: float = 0.0, **kwargs) -> None:
        """재연결 중이면 조용히 버린다.

        아이가 말하는 도중 연결이 끊기면 그 몇백 ms 는 되살릴 방법이 없다.
        버퍼에 쌓아 두었다가 재연결 후 몰아 보내면 **말이 뒤엉킨 채 도착**해서
        인형이 엉뚱한 대답을 한다. 짧은 무음이 더 자연스럽다.

        단 **activity 신호는 예외**다(wait_sec > 0). 오디오 한 조각은 없어도 말이
        되지만 턴의 시작·끝 표시가 없어지면 그 턴 전체가 무응답이 된다.
        """
        if wait_sec and not self._ready.is_set():
            await self.wait_ready(wait_sec)

        session = self._session
        if not self._ready.is_set() or session is None:
            self.dropped_chunks += 1
            return
        try:
            await session.send_realtime_input(**kwargs)
        except Exception as e:
            # 끊긴 것이다. 재연결은 events() 가 한다 — 여기서 하면 세션이 둘이 된다.
            log.warning("Live 전송 실패(연결 끊김으로 간주): %s", e)
            self._ready.clear()
            self.dropped_chunks += 1

    async def send_audio(self, pcm: bytes) -> None:
        from google.genai import types

        await self._send(
            audio=types.Blob(
                data=pcm, mime_type=f"audio/pcm;rate={config.LIVE_INPUT_RATE}"
            )
        )

    async def send_activity_start(self) -> None:
        from google.genai import types

        await self._send(
            wait_sec=config.LIVE_SIGNAL_WAIT_SEC, activity_start=types.ActivityStart()
        )

    async def send_activity_end(self) -> None:
        from google.genai import types

        await self._send(
            wait_sec=config.LIVE_SIGNAL_WAIT_SEC, activity_end=types.ActivityEnd()
        )

    # --- 다운링크 ---------------------------------------------------------
    async def events(self):
        """Gemini 의 응답을 이벤트로 흘려보낸다. 연결이 끊기면 다시 붙는다.

        yield 하는 것:
            ("audio",      bytes)   24kHz PCM. 그대로 앱에 넘긴다
            ("transcript", str)     인형이 한 말(조각). 이어붙이면 한 문장이 된다
            ("turn_complete", None) 한 턴 끝
            ("session_reset", None) 재연결됨 — 이전 맥락이 사라졌다

        모든 재시도를 소진하면 LiveUnavailableError 를 올린다.
        """
        attempt = 0
        connected_once = False

        while True:
            # ── 연결 ──────────────────────────────────────────────────
            try:
                await self._open()
            except Exception as e:
                attempt += 1
                if attempt > config.LIVE_RECONNECT_ATTEMPTS:
                    raise LiveUnavailableError(f"연결 실패 {attempt - 1}회: {e}") from e
                log.warning(
                    "Live 연결 실패 (%d/%d): %s",
                    attempt,
                    config.LIVE_RECONNECT_ATTEMPTS,
                    e,
                )
                await asyncio.sleep(_backoff(attempt))
                continue

            if connected_once:
                # 앱은 이걸 보고 "인형이 방금 대화를 잊었다"를 안다.
                log.info("Live 재연결됨 — 이전 맥락 없음")
                yield ("session_reset", None)
            connected_once = True
            attempt = 0

            # ── 수신 ──────────────────────────────────────────────────
            try:
                async for msg in self._session.receive():
                    self.usage.add(getattr(msg, "usage_metadata", None))

                    sc = getattr(msg, "server_content", None)
                    if sc is not None:
                        tr = getattr(sc, "output_transcription", None)
                        if tr is not None and tr.text:
                            yield ("transcript", tr.text)

                    # 오디오는 msg.data 로 온다(server_content 밖이다).
                    if getattr(msg, "data", None):
                        yield ("audio", msg.data)

                    if sc is not None and getattr(sc, "turn_complete", False):
                        self.usage.turns += 1
                        yield ("turn_complete", None)
            except asyncio.CancelledError:
                # 앱이 끊어서 상위가 태스크를 취소한 것이다. 재연결하면 안 된다.
                raise
            except Exception as e:
                log.warning("Live 수신 중단: %s", e)
            else:
                # receive() 가 예외 없이 끝난 것도 세션 종료다. Live 는 세션 수명
                # 상한이 있어서, 긴 대화에서는 오히려 이쪽으로 끝나는 게 정상이다.
                log.info("Live 세션이 서버 쪽에서 닫힘")

            # ── 재연결 ────────────────────────────────────────────────
            await self._shutdown()
            attempt += 1
            if attempt > config.LIVE_RECONNECT_ATTEMPTS:
                raise LiveUnavailableError(f"재연결 {attempt - 1}회 모두 실패")
            await asyncio.sleep(_backoff(attempt))
