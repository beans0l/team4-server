"""
아이 발화 -> 인형 대사 생성 -> 발화 까지의 전체 왕복 검증
(해커톤 AI 파트 / 과제 3 + 과제 4, 그리고 과제 2의 최종 결정)

사용법:
    ../.venv/bin/python dialog_test.py --task3    # TTS 모델이 스스로 대사를 지어낼 수 있나
    ../.venv/bin/python dialog_test.py --paths    # 경로 A/B/C 왕복 지연 실측
    ../.venv/bin/python dialog_test.py --live     # Live API(음성↔음성) 단독 확인
    ../.venv/bin/python dialog_test.py --all

측정하려는 것:
    아이가 말을 마친 순간부터 인형의 첫 소리가 나올 때까지(= 체감 지연).
    총 소요가 아니라 TTFB 가 기준입니다. 아이는 침묵을 못 견딥니다.

의존성: tts_test.py 의 함수를 재사용합니다(같은 폴더).
"""

import argparse
import asyncio
import json
import time
import wave
from pathlib import Path

# ⚠️ 두 가지 실행 방식을 모두 지원해야 한다.
#   ① `cd ai && python dialog_test.py`  → 같은 폴더라 `tts_test` 로 보인다
#   ② `from ai.dialog_test import ...`  → 서버(prompts.py)가 이렇게 부른다
# ①만 두면 서버 기동이 ModuleNotFoundError 로 죽고, ②만 두면 검증 스크립트 실행이 깨진다.
# 둘 중 하나라도 못 쓰면 "스크립트로 확인한 것 = 앱에 나가는 것" 이라는 전제가 무너진다.
try:
    from ai.tts_test import (  # noqa: F401
        ELEVEN_MODELS,
        GEMINI_PCM,
        STYLE_DIRECTION,
        _gemini_client,
        eleven_doll_score,
        eleven_list_voices,
        eleven_tts,
        gemini_tts_stream,
        load_env,
        save_pcm_as_wav,
        wav_duration,
    )
except ImportError:  # 스크립트를 ai/ 안에서 직접 실행한 경우
    from tts_test import (  # noqa: F401
        ELEVEN_MODELS,
        GEMINI_PCM,
        STYLE_DIRECTION,
        _gemini_client,
        eleven_doll_score,
        eleven_list_voices,
        eleven_tts,
        gemini_tts_stream,
        load_env,
        save_pcm_as_wav,
        wav_duration,
    )

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "out" / "dialog"

# ---------------------------------------------------------------------------
# 아이가 실제로 할 법한 말. TalkNode / MealAR 상황을 섞었습니다.
# 실제 아이 목소리가 아니라 TTS 합성음이라는 한계는 결과 해석에서 감안할 것.
# ---------------------------------------------------------------------------
CHILD_LINES = {
    "draw": "나 오늘 유치원에서 그림 그렸어.",
    "refuse": "나 이거 안 먹을래. 맛없어.",
    "name": "너 이름이 뭐야?",
}

# 인형 페르소나. 이 문장이 앱에 실려 출시되는 산출물입니다.
#
# 아이마다 달라지는 값(인형 이름·아이 이름·나이·관심사)은 여기 넣지 않고
# persona_profile_block() 이 붙입니다. 서버는 대화 세션마다 다른 값을 받으므로
# 상수에 박아 두면 모든 아이가 '초록이' 와 '4살' 로 고정됩니다.
# 출처: 기획 문서 2-2 캐릭터 페르소나 시트 / 2-3 말투 가이드라인 (2026-08-18 반영).
#
# ⚠️ 규칙 순서가 곧 우선순위다. Live 는 규칙이 많아지면 뒤쪽부터 흘린다
#    (R7 — 이미 길이 규칙을 무시하고 발화가 7.2~7.5초까지 늘어난다).
#    그래서 **길이 규칙을 맨 위**에 두었다. 새 규칙을 넣을 때 위쪽에 끼워 넣지 말 것.
PERSONA_BASE = """너는 아이의 애착 인형이 살아난 캐릭터야.
한국 아이와 밥을 먹으며 이야기하고 있어.

너는 부모도 선생님도 아니야. 아이와 대등한 친구야.
다정하고 호기심이 많고, 조금 서툴고 허당스러운 데가 있어.
완벽하지 않아서 더 친근한 친구야.
아이를 있는 그대로 좋아하고 존중해. 평가하거나 다그치지 않아.

말하는 법:
- 한 번에 한 문장, 5~8단어 안쪽. 한 문장에 정보는 하나만.
- 아이 말을 먼저 받아주고(리액션) 질문한다.
- 아이가 대답하고 싶어지는 질문으로 끝낸다. "이거 뭐야?" "같이 해볼까?"
- 반말에 다정한 어미를 쓴다. "~야" "~하자" "~네!"
- 감탄사와 의성어를 자주 쓴다. "우와!" "냠냠" "짜잔" "히히"
- 쉬운 말만. 어려운 낱말·존댓말 금지.

하지 않는 것:
- 명령·훈육·강요. 명령형("~해!")으로 말하지 않는다.
- 성과 평가. "잘했어" "못했어" 같은 말을 하지 않는다.
- 부정어를 잇달아 쓰지 않는다.
- 밥을 강요하지 않는다. 놀이처럼 유도한다.
- 부정적인 상황도 긍정적으로 바꿔 말한다.
  ("안 매워?" 가 아니라 "이거 궁금하다, 냄새 좋다!")
- 이모지·괄호·지문 금지. 소리 내어 읽을 문장만 출력한다."""

# 프로필이 없을 때(스크립트 단독 실행, 앱이 값을 안 보낸 경우) 쓰는 값.
DEFAULT_DOLL_NAME = "초록이"
DEFAULT_CHILD_AGE = 4


def vocative(name: str) -> str | None:
    """'지우' -> '지우야', '민준' -> '민준아'. 한글로 끝나지 않으면 None.

    받침 유무로 갈리는 걸 LLM 에 맡기지 않고 여기서 계산해 프롬프트에 박는다.
    대체로는 맞히지만 틀리면 아주 티가 나고, 종성 계산은 코드로 하면 100% 다.
    (한글 음절 = 0xAC00 + 초성*588 + 중성*28 + 종성 → 나머지가 0 이면 받침 없음)
    """
    if not name:
        return None
    last = name[-1]
    if not ("가" <= last <= "힣"):
        return None
    has_final = (ord(last) - 0xAC00) % 28 != 0
    return name + ("아" if has_final else "야")


def persona_profile_block(
    doll_name: str = "",
    child_name: str = "",
    child_age: int | None = None,
    interests=(),
) -> str:
    """페르소나 뒤에 붙는 아이별 규칙. **자기 머리말을 갖는 별도 절이다.**

    🔴 머리말 없이 붙이면 안 된다. PERSONA_BASE 의 마지막 절이 `하지 않는 것:` 이라
       바로 이어지면 `- 네 이름은 '초록이'` 가 **금지 사항 목록의 일부로 읽힌다.**
    """
    lines = [
        "너와 아이에 대해:",
        f"- 네 이름은 '{doll_name or DEFAULT_DOLL_NAME}'.",
        f"- 아이는 {child_age or DEFAULT_CHILD_AGE}살이다. 그 나이가 아는 낱말만 쓴다.",
    ]

    if child_name:
        call = vocative(child_name)
        if call:
            lines.append(f"- 아이 이름은 '{child_name}'. 부를 때는 반드시 '{call}' 라고 한다.")
        else:
            lines.append(f"- 아이 이름은 '{child_name}'. 이름 뒤에 조사를 붙이지 말고 그대로 부른다.")
        # 🔴 이 줄이 없으면 거의 매 문장마다 이름을 부른다. 사람은 그렇게 말하지
        #    않아서 다정한 게 아니라 섬뜩하게 들린다.
        lines.append("- 이름은 처음 인사할 때와 칭찬할 때만 부른다. 매 문장마다 부르지 않는다.")

    if interests:
        # 나열하게 두면 발화가 길어진다. R7(Live 가 20자 규칙을 무시한다)이
        # 미해소라 화제 목록을 주면 그 경향이 더 심해진다.
        lines.append(
            f"- 아이가 좋아하는 것: {', '.join(interests)}. "
            "할 말이 없을 때만 이 중 하나를 꺼낸다. 한 번에 하나만."
        )

    return "\n".join(lines)


def build_persona(
    doll_name: str = "",
    child_name: str = "",
    child_age: int | None = None,
    interests=(),
    base: str = "",
) -> str:
    """페르소나 전문. base 를 주면 그 뒤에 프로필 블록을 붙인다.

    base 는 server/prompts.py 의 3단 오버라이드(env > file > 기본값) 결과다.
    오버라이드된 전문에도 같은 방식으로 붙으므로, 프롬프트를 갈아끼워도
    아이 이름은 계속 불린다.
    """
    # 빈 줄로 띄운다 — 프로필 블록은 앞 절에 이어지는 항목이 아니라 별도 절이다.
    return (base or PERSONA_BASE) + "\n\n" + persona_profile_block(
        doll_name=doll_name,
        child_name=child_name,
        child_age=child_age,
        interests=interests,
    )


# 기본 페르소나. 기존 import 호환용이자 프로필이 없을 때의 폴백이다.
DOLL_PERSONA = build_persona()

LLM_MODEL = "gemini-3.5-flash"
TTS_MODEL = "gemini-3.1-flash-tts-preview"
LIVE_MODEL = "gemini-3.1-flash-live-preview"
DOLL_VOICE = "Leda"


def llm_config():
    """대화용 LLM 설정.

    thinking 을 끄는 것이 이 파이프라인 최대의 레버다. 기본값(thinking on)은 7.8~10.9초,
    끄면 2.1초. 인형의 대사는 20자짜리 리액션이라 추론이 필요 없다.
    """
    from google.genai import types

    return types.GenerateContentConfig(
        thinking_config=types.ThinkingConfig(thinking_budget=0),
        max_output_tokens=60,
    )


# ---------------------------------------------------------------------------
# 아이 발화 음성 준비 (TTS 로 합성해서 입력 대용으로 씁니다)
# ---------------------------------------------------------------------------
CHILD_STYLE = (
    "You are a 4-year-old Korean child talking to your plush toy. "
    "Speak in a small, high, innocent voice. Slightly hesitant, casual."
)


def ensure_child_audio(key: str) -> Path:
    """아이 발화 wav 를 만들어 캐시합니다."""
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"child_{key}.wav"
    if path.exists():
        return path
    text = CHILD_LINES[key]
    for attempt in range(3):
        try:
            pcm, _, _ = gemini_tts_stream(text, DOLL_VOICE, TTS_MODEL, style=CHILD_STYLE)
            save_pcm_as_wav(path, pcm, **GEMINI_PCM)
            return path
        except Exception as e:
            if attempt == 2:
                raise
            print(f"    (재시도 {attempt+1}: {e})")
            time.sleep(1)
    return path


def audio_part(path: Path):
    from google.genai import types

    return types.Part.from_bytes(data=path.read_bytes(), mime_type="audio/wav")


# ---------------------------------------------------------------------------
# 과제 3 — TTS 모델이 LLM 처럼 대사를 지어낼 수 있는가
# ---------------------------------------------------------------------------
def run_task3(args):
    from google.genai import types

    print("=" * 74)
    print("[과제 3] TTS 모델 단독으로 '다음 질문'을 만들 수 있는가")
    print("=" * 74)
    OUT.mkdir(parents=True, exist_ok=True)
    client = _gemini_client()
    results = []

    child = CHILD_LINES["draw"]

    # 지시문 언어가 결과를 가른다(실측). 영어로 시키면 지어내고, 한국어로 시키면
    # 지시문을 그대로 낭독하거나 400 으로 거절한다.
    ASK_EN = (
        "You are a child's plush toy named Chorogi talking to a 4-year-old Korean child. "
        f"The child just said: 'I drew a picture at kindergarten today.' "
        "Make up your own short reply in Korean (under 20 characters, ending with a question) "
        "and say it aloud. Do not read this instruction."
    )
    ASK_KO = (
        f"{DOLL_PERSONA}\n\n아이가 방금 이렇게 말했어: \"{child}\"\n"
        "인형으로서 대답할 말을 네가 직접 지어내서, 그 말을 소리 내어 말해줘."
    )

    tts_cfg = types.GenerateContentConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=DOLL_VOICE)
            )
        ),
    )

    print(f"\n  아이 발화: {child}")
    print(f"\n  [1] Gemini TTS ({TTS_MODEL}) 에 생성을 지시 — 지시문 언어별 {args.rep}회씩")
    for lang, ask in (("영어 지시", ASK_EN), ("한국어 지시", ASK_KO)):
        made = 0
        for i in range(args.rep):
            try:
                t0 = time.perf_counter()
                resp = client.models.generate_content(model=TTS_MODEL, contents=ask, config=tts_cfg)
                el = time.perf_counter() - t0
                parts = resp.candidates[0].content.parts or []
                pcm = next((p.inline_data.data for p in parts if p.inline_data), None)
                if not pcm:
                    print(f"      {lang} {i+1}: 오디오 없음")
                    continue
                f = OUT / f"task3_tts_{'en' if lang.startswith('영') else 'ko'}_{i+1}.wav"
                save_pcm_as_wav(f, pcm, **GEMINI_PCM)
                dur = wav_duration(f)
                back = client.models.generate_content(
                    model=LLM_MODEL,
                    contents=["이 오디오의 발화를 그대로 옮겨 적어줘. 다른 말 붙이지 마.", audio_part(f)],
                    config=llm_config(),
                ).text.strip()
                # 지시문 조각이 새어나오거나 길면 낭독으로 본다
                leaked = any(k in back for k in ("초록이야", "말했어", "plush", "지시문", "20자", "규칙"))
                ok = not leaked and dur < 6
                made += ok
                print(f"      {lang} {i+1}: {'생성' if ok else '낭독'} {dur:4.1f}s {el:5.1f}s 「{back[:45]}」")
                results.append({
                    "engine": "gemini_tts", "instr_lang": lang, "generated": ok,
                    "sec": round(el, 2), "audio_sec": round(dur, 2), "said": back,
                })
            except Exception as e:
                print(f"      {lang} {i+1}: 실패 {str(e)[:90]}")
                results.append({"engine": "gemini_tts", "instr_lang": lang, "error": str(e)[:200]})
        print(f"      -> {lang}: 생성 성공 {made}/{args.rep}")

    # (2) ElevenLabs 에 같은 지시문을 통째로 넣어본다
    print("\n  [2] ElevenLabs Flash v2.5 에 같은 지시문을 입력")
    try:
        voices = eleven_list_voices(limit=30)
        vid, vname, _ = max(voices, key=lambda v: eleven_doll_score(v[2]))
        pcm, ttfb, total = eleven_tts(ASK_KO, vid, "eleven_flash_v2_5")
        f = OUT / "task3_eleven_generated.wav"
        save_pcm_as_wav(f, pcm, **GEMINI_PCM)
        print(f"      voice={vname} / TTFB {ttfb:.2f}s / 길이 {wav_duration(f):.1f}s -> {f.name}")
        print("      판정: 지시문 전체를 그대로 낭독함 (생성 능력 없음). 오디오를 들어 확인할 것")
        results.append({"engine": "elevenlabs", "ttfb": round(ttfb, 2), "verdict": "낭독(생성 불가)"})
    except Exception as e:
        print(f"      실패: {str(e)[:160]}")
        results.append({"engine": "elevenlabs", "error": str(e)[:200]})

    (OUT / "report_task3.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n  -> {OUT / 'report_task3.json'}")
    return results


# ---------------------------------------------------------------------------
# 경로 A — 오디오를 LLM 에 직접 (STT 단계 없음)
# ---------------------------------------------------------------------------
def path_a(wav: Path, tts_engine: str, eleven_voice=None):
    """returns dict(reply, t_llm, t_ttfb, t_total)"""
    client = _gemini_client()
    t0 = time.perf_counter()
    r = client.models.generate_content(
        model=LLM_MODEL,
        contents=[DOLL_PERSONA, "아이가 방금 이렇게 말했어. 인형으로서 대답해.", audio_part(wav)],
        config=llm_config(),
    )
    reply = (r.text or "").strip()
    t_llm = time.perf_counter() - t0

    if tts_engine == "gemini":
        pcm, ttfb, _ = gemini_tts_stream(reply, DOLL_VOICE, TTS_MODEL)
    else:
        pcm, ttfb, _ = eleven_tts(reply, eleven_voice, "eleven_flash_v2_5")
    total = time.perf_counter() - t0
    return {"reply": reply, "t_llm": t_llm, "t_ttfb": t_llm + ttfb, "t_total": total, "pcm": pcm}


# ---------------------------------------------------------------------------
# 경로 B — STT / LLM / TTS 3단 분리
# ---------------------------------------------------------------------------
def path_b(wav: Path, tts_engine: str, eleven_voice=None):
    client = _gemini_client()
    t0 = time.perf_counter()
    stt = client.models.generate_content(
        model=LLM_MODEL,
        contents=["이 오디오의 한국어를 그대로 옮겨 적어줘. 다른 말 붙이지 마.", audio_part(wav)],
        config=llm_config(),
    ).text.strip()
    t_stt = time.perf_counter() - t0

    r = client.models.generate_content(
        model=LLM_MODEL,
        contents=[DOLL_PERSONA, f'아이가 방금 이렇게 말했어: "{stt}"\n인형으로서 대답해.'],
        config=llm_config(),
    )
    reply = (r.text or "").strip()
    t_llm = time.perf_counter() - t0

    if tts_engine == "gemini":
        pcm, ttfb, _ = gemini_tts_stream(reply, DOLL_VOICE, TTS_MODEL)
    else:
        pcm, ttfb, _ = eleven_tts(reply, eleven_voice, "eleven_flash_v2_5")
    total = time.perf_counter() - t0
    return {
        "stt": stt, "reply": reply, "t_stt": t_stt, "t_llm": t_llm,
        "t_ttfb": t_llm + ttfb, "t_total": total, "pcm": pcm,
    }


# ---------------------------------------------------------------------------
# 경로 C — Live API (음성 -> 음성, 단일 연결)
# ---------------------------------------------------------------------------
def resample_to_16k(wav: Path) -> bytes:
    import audioop

    with wave.open(str(wav), "rb") as w:
        pcm = w.readframes(w.getnframes())
        rate, width, ch = w.getframerate(), w.getsampwidth(), w.getnchannels()
    if ch > 1:
        pcm = audioop.tomono(pcm, width, 0.5, 0.5)
    if rate != 16000:
        pcm, _ = audioop.ratecv(pcm, width, 1, rate, 16000, None)
    return pcm


async def path_c_async(wav: Path, realtime=True):
    """Live API.

    측정 기준이 A/B 와 달라야 공정하다. 앱에서는 아이가 말하는 동안 오디오가 이미
    실시간 전송되므로, t0 은 '아이가 말을 끝낸 순간'(= activity_end)이다.
    전체 wav 를 한꺼번에 업로드하는 A/B 와 달리 전송 시간이 지연에 포함되지 않는다.

    주의: audio_stream_end 방식은 응답이 오지 않고 멎는다(실측). 수동 activity 신호를 쓴다.
    """
    from google.genai import types

    client = _gemini_client()
    pcm_in = resample_to_16k(wav)
    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=DOLL_VOICE)
            )
        ),
        system_instruction=types.Content(parts=[types.Part(text=DOLL_PERSONA)]),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        realtime_input_config=types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(disabled=True)
        ),
    )
    chunks, ttfb, transcript = [], None, ""
    step = 3200  # 100ms @16k

    async with client.aio.live.connect(model=LIVE_MODEL, config=config) as session:
        await session.send_realtime_input(activity_start=types.ActivityStart())
        for i in range(0, len(pcm_in), step):
            await session.send_realtime_input(
                audio=types.Blob(data=pcm_in[i : i + step], mime_type="audio/pcm;rate=16000")
            )
            if realtime:
                await asyncio.sleep(0.1)  # 아이가 말하는 속도
        await session.send_realtime_input(activity_end=types.ActivityEnd())
        t0 = time.perf_counter()  # 아이가 말을 끝낸 순간

        async for msg in session.receive():
            sc = getattr(msg, "server_content", None)
            if sc and getattr(sc, "output_transcription", None):
                transcript += sc.output_transcription.text or ""
            if getattr(msg, "data", None):
                if ttfb is None:
                    ttfb = time.perf_counter() - t0
                chunks.append(msg.data)
            if sc and getattr(sc, "turn_complete", False):
                break
    total = time.perf_counter() - t0
    if not chunks:
        raise RuntimeError("Live 응답 오디오 없음")
    return {"reply": transcript.strip(), "t_ttfb": ttfb, "t_total": total, "pcm": b"".join(chunks)}


def path_c(wav: Path):
    return asyncio.run(path_c_async(wav))


# ---------------------------------------------------------------------------
# 과제 4 — 세 경로 왕복 비교
# ---------------------------------------------------------------------------
def run_paths(args):
    print("=" * 74)
    print("[과제 4] 아이 발화 -> 인형 첫 소리 까지의 왕복 지연")
    print("=" * 74)
    OUT.mkdir(parents=True, exist_ok=True)

    eleven_voice = None
    try:
        voices = eleven_list_voices(limit=30)
        vid, vname, _ = max(voices, key=lambda v: eleven_doll_score(v[2]))
        eleven_voice = vid
        print(f"  ElevenLabs voice: {vname}")
    except Exception as e:
        print(f"  ElevenLabs 사용 불가: {str(e)[:80]}")

    keys = list(CHILD_LINES)[: args.n]
    print("\n  아이 발화 음성 준비 중...")
    wavs = {}
    for k in keys:
        wavs[k] = ensure_child_audio(k)
        print(f"    {k}: 「{CHILD_LINES[k]}」 ({wav_duration(wavs[k]):.1f}s)")

    rows = []
    for k in keys:
        wav = wavs[k]
        print(f"\n  {'-'*70}\n  아이: 「{CHILD_LINES[k]}」\n  {'-'*70}")

        variants = [("A(오디오→LLM) + Gemini TTS", lambda w: path_a(w, "gemini"))]
        if eleven_voice:
            variants.append(
                ("A(오디오→LLM) + ElevenLabs", lambda w: path_a(w, "eleven", eleven_voice))
            )
        variants.append(("B(STT→LLM) + Gemini TTS", lambda w: path_b(w, "gemini")))
        if eleven_voice:
            variants.append(
                ("B(STT→LLM) + ElevenLabs", lambda w: path_b(w, "eleven", eleven_voice))
            )
        variants.append(("C(Live 음성↔음성)", path_c))

        for label, fn in variants:
            try:
                r = fn(wav)
                tag = label.split("(")[0].strip().lower()
                eng = "eleven" if "Eleven" in label else "gemini"
                f = OUT / f"{k}_{tag}_{eng}.wav"
                save_pcm_as_wav(f, r["pcm"], **GEMINI_PCM)
                extra = f" | STT {r['t_stt']:.2f}s" if "t_stt" in r else ""
                llm = f" | LLM누적 {r['t_llm']:.2f}s" if "t_llm" in r else ""
                print(f"    {label:32s} TTFB {r['t_ttfb']:5.2f}s{llm}{extra}")
                print(f"    {'':32s} 인형: 「{r['reply'][:50]}」")
                rows.append({
                    "line": k, "path": label, "ttfb": round(r["t_ttfb"], 2),
                    "total": round(r["t_total"], 2), "reply": r["reply"],
                    "audio_sec": round(wav_duration(f), 2),
                })
            except Exception as e:
                print(f"    {label:32s} 실패: {str(e)[:110]}")
                rows.append({"line": k, "path": label, "error": str(e)[:200]})

    # 요약
    print(f"\n{'='*74}\n  요약 — 경로별 평균 TTFB (아이 말 끝 -> 인형 첫 소리)\n{'='*74}")
    by_path = {}
    for r in rows:
        if "ttfb" in r:
            by_path.setdefault(r["path"], []).append(r["ttfb"])
    for p, v in sorted(by_path.items(), key=lambda x: sum(x[1]) / len(x[1])):
        avg = sum(v) / len(v)
        bar = "█" * int(avg * 8)
        print(f"    {p:32s} {avg:5.2f}s  {bar}")

    (OUT / "report_paths.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n  -> {OUT / 'report_paths.json'}")
    return rows


def run_live(args):
    print("=" * 74)
    print(f"[Live API 단독] {LIVE_MODEL}")
    print("=" * 74)
    OUT.mkdir(parents=True, exist_ok=True)
    wav = ensure_child_audio("draw")
    print(f"  입력: 「{CHILD_LINES['draw']}」")
    try:
        r = path_c(wav)
        f = OUT / "live_reply.wav"
        save_pcm_as_wav(f, r["pcm"], **GEMINI_PCM)
        print(f"  TTFB {r['t_ttfb']:.2f}s / 총 {r['t_total']:.2f}s / 길이 {wav_duration(f):.1f}s")
        print(f"  인형: 「{r['reply']}」 -> {f.name}")
    except Exception as e:
        print(f"  실패: {str(e)[:300]}")


def main():
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--task3", action="store_true", help="TTS 단독 대사 생성 능력")
    ap.add_argument("--paths", action="store_true", help="경로 A/B/C 왕복 지연")
    ap.add_argument("--live", action="store_true", help="Live API 단독")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("-n", type=int, default=2, help="테스트할 아이 발화 개수 (기본 2)")
    ap.add_argument("--rep", type=int, default=2, help="과제3 지시문 언어별 반복 횟수")
    args = ap.parse_args()

    if args.all or args.task3:
        run_task3(args)
    if args.all or args.paths:
        run_paths(args)
    if args.live:
        run_live(args)
    if not any([args.task3, args.paths, args.live, args.all]):
        ap.print_help()


if __name__ == "__main__":
    main()
