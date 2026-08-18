"""WS /doll/talk E2E 검증 클라이언트.

앱이 할 일을 그대로 흉내낸다 — wav 를 16kHz PCM 으로 만들어 100ms 씩 실시간 속도로
보내고, activity_end 를 친 뒤 인형의 첫 소리가 언제 오는지 잰다.

    # 서버를 먼저 띄운다
    ../.venv/bin/python -m uvicorn server.main:app --port 8000

    # ① 프로토콜 배선만 (오디오를 안 보내므로 사실상 무료)
    ../.venv/bin/python -m ai.talk_client --protocol-only

    # ② 단발 왕복 — TTFB 가 dialog_test 실측(0.85초)과 맞는지
    ../.venv/bin/python -m ai.talk_client ai/out/dialog/child_draw.wav

    # ③ 장시간 세션 — R5(Live 비용) + 세션 한계를 한 번에 잰다
    ../.venv/bin/python -m ai.talk_client ai/out/dialog/child_draw.wav --soak 18m

⚠️ ②③ 은 실제 Gemini 크레딧을 쓴다. 무료 크레딧이 $10 뿐이고 R5 가 미측정이라
   ③ 은 하루에 한 번으로 족하다. 스프라이트 실호출과 같은 날 몰아 하지 말 것.

측정 기준은 dialog_test.py 와 같다: **아이가 말을 끝낸 순간(activity_end) →
인형의 첫 오디오 프레임.** 총 소요가 아니다. 아이는 침묵을 못 견딘다.
서버 중계가 한 단 끼므로 순수 Live(0.85초)보다 조금 길게 나오는 게 정상이다.
"""

import argparse
import asyncio
import json
import time
from pathlib import Path

try:
    from ai.dialog_test import resample_to_16k
    from ai.tts_test import save_pcm_as_wav
except ImportError:  # ai/ 안에서 직접 실행한 경우
    from dialog_test import resample_to_16k
    from tts_test import save_pcm_as_wav

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "out" / "talk"

DEFAULT_URL = "ws://127.0.0.1:8000/doll/talk"

CHUNK_MS = 100
CHUNK_BYTES = 3200  # 100ms @ 16kHz / 16-bit / mono
OUT_RATE = 24000  # 서버가 내려주는 오디오

# --protocol-only 가 오류 프레임을 기다리는 시간.
# 서버의 재연결(3회, 백오프 합 3.5초)이 끝나고 error 가 올 때까지 기다려야 한다.
# 실측: 잘못된 키에서 error 프레임까지 약 5.5초.
PROTOCOL_WAIT_SEC = 12


def _parse_duration(text: str) -> float:
    """'18m' / '90s' / '90' -> 초."""
    text = text.strip().lower()
    if text.endswith("m"):
        return float(text[:-1]) * 60
    if text.endswith("s"):
        return float(text[:-1])
    return float(text)


async def _one_turn(ws, pcm: bytes, realtime: bool) -> dict:
    """한 턴: 오디오 전송 -> activity_end -> 응답 수신 -> turn_complete."""
    import websockets

    await ws.send(json.dumps({"type": "activity_start"}))
    for i in range(0, len(pcm), CHUNK_BYTES):
        await ws.send(pcm[i : i + CHUNK_BYTES])
        if realtime:
            # 아이가 말하는 속도. 이걸 빼면 업로드가 순식간에 끝나서 실제 앱과
            # 다른 조건이 된다(Live 의 처리 시작 시점이 달라진다).
            await asyncio.sleep(CHUNK_MS / 1000)
    await ws.send(json.dumps({"type": "activity_end"}))

    t0 = time.perf_counter()  # ← 측정 시작: 아이가 말을 끝낸 순간
    audio, transcript, ttfb, resets = [], "", None, 0

    while True:
        try:
            msg = await asyncio.wait_for(ws.recv(), timeout=35)
        except asyncio.TimeoutError:
            raise RuntimeError(
                "35초 안에 응답이 없다. activity_end 가 서버에 닿았는지, "
                "서버 로그에 Live 연결 오류가 없는지 확인할 것."
            )
        except websockets.exceptions.ConnectionClosed as e:
            raise RuntimeError(f"서버가 연결을 닫았다 (code={e.code}): {e.reason}")

        if isinstance(msg, bytes):
            if ttfb is None:
                ttfb = time.perf_counter() - t0
            audio.append(msg)
            continue

        evt = json.loads(msg)
        kind = evt.get("type")
        if kind == "transcript":
            transcript += evt.get("text", "")
        elif kind == "session_reset":
            resets += 1
            print("    ⚠️ session_reset — 인형이 이전 대화를 잊었다")
        elif kind == "error":
            raise RuntimeError(f"서버 오류 프레임: {evt['code']} — {evt['message']}")
        elif kind == "turn_complete":
            break

    return {
        "ttfb": ttfb,
        "total": time.perf_counter() - t0,
        "transcript": transcript.strip(),
        "pcm": b"".join(audio),
        "resets": resets,
    }


async def run_protocol_only(url: str) -> int:
    """프레임 규약만 확인한다.

    붙자마자 끊는다. 오디오도 activity 신호도 보내지 않으므로 Gemini 는 아무것도
    생성하지 않는다. 확인하는 것: 서버가 WS 핸드셰이크를 받아주는가, 키가 없을 때
    error 프레임이 **JSON 으로** 오는가(앱이 code 로 분기할 수 있어야 한다).

    ⚠️ "완전 무료"는 아니다. 서버는 앱이 붙는 즉시 Live 연결을 연다 — 첫 발화에
       연결 시간을 얹지 않기 위해서다. 토큰은 거의 안 나가지만 빈 세션이 하나
       열렸다 닫힌다. 정말로 한 푼도 안 쓰고 배선만 보려면 키 없이 서버를 띄운다:
           GEMINI_API_KEY= uvicorn server.main:app --port 8000
       그러면 LIVE_UNAVAILABLE 프레임이 오고, 그 경로까지 여기서 확인된다.
    """
    import websockets

    print(f"[프로토콜] {url}")
    try:
        async with websockets.connect(url, max_size=None) as ws:
            print("  ✅ 핸드셰이크 성공")
            # ⚠️ 짧게 기다리면 안 된다. 키가 **틀린** 경우 서버는 재연결을 3회
            #    시도한 뒤(백오프 합 3.5초 + 시도 시간) error 프레임을 보낸다.
            #    3초만 기다렸다가 "정상"으로 판정한 적이 있다 — 검증 도구가 거짓
            #    합격을 내면 서버가 고장난 것보다 나쁘다.
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=PROTOCOL_WAIT_SEC)
            except asyncio.TimeoutError:
                print(
                    f"  ✅ {PROTOCOL_WAIT_SEC}초 동안 오류 프레임 없음 "
                    "— 정상 (키가 유효하고 Live 연결이 섰다)"
                )
                return 0

            if isinstance(msg, bytes):
                print(f"  ❌ 아무것도 안 보냈는데 오디오 {len(msg)}바이트가 왔다")
                return 1
            evt = json.loads(msg)
            if evt.get("type") == "error":
                print(f"  ⚠️ error 프레임: {evt['code']} — {evt['message']}")
                print("     키가 없거나 틀린 서버라면 여기까지가 정상이다")
                print("     (앱이 code 로 분기할 수 있다는 것까지 확인됨)")
                return 0
            print(f"  ❌ 예상 밖의 프레임: {evt}")
            return 1
    except websockets.exceptions.InvalidStatus as e:
        print(f"  ❌ 핸드셰이크 거절: {e}")
        print("     서버가 /doll/talk 를 등록했는지(main.py include_router) 확인할 것")
        return 1
    except OSError as e:
        print(f"  ❌ 서버에 접속할 수 없다: {e}")
        return 1


async def run_talk(url: str, wav: Path, soak_sec: float, realtime: bool) -> int:
    import websockets

    if not wav.exists():
        print(f"❌ 입력 wav 가 없다: {wav}")
        print("   먼저 만들 것:  ../.venv/bin/python dialog_test.py --paths")
        return 1

    pcm_in = resample_to_16k(wav)
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"[대화] {url}")
    print(f"  입력 {wav.name} — {len(pcm_in) / 32000:.1f}초 @16kHz")
    if soak_sec:
        print(f"  소크 {soak_sec / 60:.0f}분 — 턴을 반복하며 세션을 유지한다")

    ttfbs, turns, resets = [], 0, 0
    started = time.perf_counter()

    try:
        async with websockets.connect(url, max_size=None) as ws:
            while True:
                turns += 1
                r = await _one_turn(ws, pcm_in, realtime)
                ttfbs.append(r["ttfb"])
                resets += r["resets"]

                path = OUT / f"reply_{turns:03d}.wav"
                save_pcm_as_wav(path, r["pcm"], rate=OUT_RATE)
                secs = len(r["pcm"]) / (OUT_RATE * 2)
                print(
                    f"  턴{turns:3d}  TTFB {r['ttfb']:.2f}초  응답 {secs:.1f}초  "
                    f"→ {path.name}"
                )
                print(f"         「{r['transcript']}」")

                # R7: 20자 규칙이 지켜지는지. 실측에서 7.2~7.5초까지 늘어졌다.
                if secs > 4.0:
                    print(f"         ⚠️ 발화가 {secs:.1f}초로 길다 (R7) — 아이가 대답할 틈이 없다")

                elapsed = time.perf_counter() - started
                if not soak_sec or elapsed >= soak_sec:
                    break
                await asyncio.sleep(2)  # 아이가 다음 말을 하기까지의 간격
    except websockets.exceptions.ConnectionClosed as e:
        print(f"  ❌ 연결이 닫혔다 (code={e.code}): {e.reason}")
        return 1
    except RuntimeError as e:
        print(f"  ❌ {e}")
        return 1
    except OSError as e:
        print(f"  ❌ 서버에 접속할 수 없다: {e}")
        return 1

    elapsed = time.perf_counter() - started
    print("-" * 70)
    print(f"  턴 {turns}회 / {elapsed / 60:.1f}분 / session_reset {resets}회")
    print(
        f"  TTFB  평균 {sum(ttfbs) / len(ttfbs):.2f}초  "
        f"최소 {min(ttfbs):.2f}  최대 {max(ttfbs):.2f}"
    )
    print("  기준: 1.2초 이하 (순수 Live 실측 0.85초 + 서버 중계 오버헤드)")
    print()
    print("  🔴 R5(Live 비용)는 **서버 로그**에서 확인한다 — 세션이 끝나면")
    print("     `Live 세션 종료 — usage={...}` 한 줄이 찍힌다.")
    print("     total_sum(델타 가정)과 last_max(누적 가정) 중 어느 쪽이 맞는지는")
    print("     턴이 여러 번인 이 소크 결과라야 갈린다.")
    return 0


def _with_profile(url: str, args) -> str:
    """--child/--age/--interests/--doll 을 쿼리스트링으로 붙인다.

    한글이 그대로 들어가면 서버에 따라 깨지므로 반드시 percent-encoding 한다.
    앱(OkHttp)도 같은 방식으로 인코딩해야 한다.
    """
    from urllib.parse import urlencode

    fields = {
        "child": args.child,
        "age": args.age,
        "interests": args.interests,
        "doll": args.doll,
    }
    query = urlencode({k: v for k, v in fields.items() if v})
    if not query:
        return url
    return f"{url}{'&' if '?' in url else '?'}{query}"


def main() -> int:
    p = argparse.ArgumentParser(description="WS /doll/talk E2E 검증")
    p.add_argument("wav", nargs="?", help="아이 발화 wav (없으면 --protocol-only 만 가능)")
    p.add_argument("--url", default=DEFAULT_URL)
    p.add_argument("--protocol-only", action="store_true", help="Live 호출 없이 규약만 확인(무료)")
    p.add_argument("--soak", help="장시간 세션. 예: 18m")
    p.add_argument(
        "--fast",
        action="store_true",
        help="오디오를 실시간 속도가 아니라 즉시 전송(디버깅용, 실측값은 못 믿는다)",
    )
    # 아이 프로필. 앱이 회원가입(이름·나이)과 관심사 입력에서 받아 붙이는 값과
    # 같은 쿼리 파라미터다. 인형이 실제로 이름을 부르는지 귀로 확인하는 용도.
    p.add_argument("--child", help="아이 이름 (예: 지우)")
    p.add_argument("--age", help="아이 나이 (예: 4)")
    p.add_argument("--interests", help="관심사, 쉼표 구분 (예: 공룡,딸기)")
    p.add_argument("--doll", help="인형 이름 (예: 초록이)")
    args = p.parse_args()

    url = _with_profile(args.url, args)

    if args.protocol_only:
        return asyncio.run(run_protocol_only(url))

    if not args.wav:
        p.error("wav 를 주거나 --protocol-only 를 쓸 것")

    return asyncio.run(
        run_talk(
            url,
            Path(args.wav),
            _parse_duration(args.soak) if args.soak else 0,
            realtime=not args.fast,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
