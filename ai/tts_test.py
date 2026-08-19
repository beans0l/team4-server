"""
인형 캐릭터 목소리(TTS) 후보 비교 테스트
(해커톤 AI 파트 / 과제 2 검증용)

사용법:
    ../.venv/bin/python tts_test.py --voices     # 1단계: Gemini voice 스크리닝 (어떤 목소리가 인형답나)
    ../.venv/bin/python tts_test.py --engines    # 2단계: 엔진 비교 (Gemini / OpenAI / ElevenLabs)
    ../.venv/bin/python tts_test.py --lipsync out/tts/xxx.wav   # 3단계: 립싱크 실현성 검증

결과: out/tts/ 폴더에 wav + report.json (TTFB / 총 지연 / 추정 비용)

의존성: google-genai 만 필요. OpenAI/ElevenLabs 는 표준 라이브러리(urllib)로 REST 직접 호출.
"""

import argparse
import json
import os
import struct
import sys
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "out" / "tts"


# ---------------------------------------------------------------------------
# .env 로더 (doll_stylize_test.py 와 동일)
# ---------------------------------------------------------------------------
def load_env():
    for candidate in (ROOT / ".env", ROOT.parent / ".env"):
        if not candidate.exists():
            continue
        for line in candidate.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip("\"'"))


# ---------------------------------------------------------------------------
# 테스트 대사 - 기획서(MealAR / TalkNode)의 실제 문장을 씁니다.
# 짧은 문장 위주여야 대화 지연이 현실적으로 측정됩니다.
# ---------------------------------------------------------------------------
LINES = {
    "greet": "안녕! 오늘 나랑 같이 밥 먹을까?",
    "praise": "우와, 당근도 잘 먹네! 정말 대단하다!",
    "sing": "밥 먹는 동안 내가 노래 불러줄게!",
}

# 캐릭터 연기 지시. Gemini TTS / OpenAI TTS 는 이 자연어 지시를 실제로 반영합니다.
# ElevenLabs Flash v2.5 는 이런 스타일 지시를 지원하지 않습니다(보이스 자체로 결정).
STYLE_DIRECTION = (
    "You are a small, soft plush-toy friend who has just come to life. "
    "You are talking to a 4-year-old Korean child during mealtime. "
    "Speak in a bright, high-pitched, playful and warm voice. "
    "Slightly slower than normal, with gentle excitement and a smile in your voice. "
    "Never sound like a news anchor or a narrator."
)

# Gemini 30개 voice 전체. 특성 라벨은 공식 문서 값 그대로.
#   https://ai.google.dev/gemini-api/docs/speech-generation
#
# tier 1 = 인형 친구 후보 (밝음/어림/친근/활발)
# tier 2 = 애매 - 들어볼 가치는 있음
# tier 3 = 부적합 예상 (내레이터/단호함/중후함) - 기본 실행에서 제외
#
# ⚠️ tier 는 라벨만 보고 매긴 것이지 소리를 듣고 매긴 게 아닙니다.
#    --all 로 전부 생성해서 직접 확인하세요. 30개 다 돌려도 약 130원입니다.
GEMINI_VOICES = [
    # (voice, 특성, tier)
    ("Leda", "Youthful", 1),
    ("Fenrir", "Excitable", 1),
    ("Puck", "Upbeat", 1),
    ("Laomedeia", "Upbeat", 1),
    ("Sadachbia", "Lively", 1),
    ("Achird", "Friendly", 1),
    ("Zephyr", "Bright", 1),
    ("Autonoe", "Bright", 1),
    ("Sulafat", "Warm", 1),
    ("Achernar", "Soft", 1),
    ("Vindemiatrix", "Gentle", 2),
    ("Callirrhoe", "Easy-going", 2),
    ("Umbriel", "Easy-going", 2),
    ("Aoede", "Breezy", 2),
    ("Zubenelgenubi", "Casual", 2),
    ("Pulcherrima", "Forward", 2),
    ("Despina", "Smooth", 2),
    ("Algieba", "Smooth", 2),
    ("Enceladus", "Breathy", 2),
    ("Iapetus", "Clear", 3),
    ("Erinome", "Clear", 3),
    ("Schedar", "Even", 3),
    ("Kore", "Firm", 3),
    ("Orus", "Firm", 3),
    ("Alnilam", "Firm", 3),
    ("Charon", "Informative", 3),
    ("Rasalgethi", "Informative", 3),
    ("Sadaltager", "Knowledgeable", 3),
    ("Gacrux", "Mature", 3),
    ("Algenib", "Gravelly", 3),
]

# 같은 voice 로 상황별 연기를 바꾸는 실험.
# 앱에서는 인형의 상태(칭찬/졸림/노래)에 따라 이 지시문만 갈아끼우면 됩니다.
# voice_name 은 그대로 두고 프롬프트만 바뀝니다.
STYLE_VARIANTS = {
    # 대조군 - 연기 지시를 아예 안 줬을 때 기본값이 뭔지 확인
    "none": "",
    # 현재 기본값
    "friend": STYLE_DIRECTION,
    # MealReport / 아이가 잘 먹었을 때
    "excited": (
        "You are a small plush-toy friend talking to a 4-year-old child. "
        "You are bursting with joy and pride because the child just ate really well. "
        "Speak fast, high-pitched, almost squealing with delight. Lots of energy."
    ),
    # 표정 스프라이트 sleepy 와 짝
    "sleepy": (
        "You are a small plush-toy friend talking to a 4-year-old child. "
        "You are very sleepy and cozy, almost dozing off. "
        "Speak slowly and softly, with a warm drowsy voice, stretching your words a little."
    ),
    # 기획: '밥 먹는 동안 노래를 불러줄게'
    "singsong": (
        "You are a small plush-toy friend talking to a 4-year-old child. "
        "Speak in a playful sing-song way, like you are almost humming a nursery rhyme. "
        "Bouncy rhythm, cheerful melody in your voice."
    ),
    # 조용히 집중시킬 때
    "whisper": (
        "You are a small plush-toy friend talking to a 4-year-old child. "
        "Whisper softly as if sharing a fun little secret, but stay warm and friendly."
    ),
}

GEMINI_TTS_MODELS = [
    "gemini-3.1-flash-tts-preview",   # 최신. 스트리밍 지원
    "gemini-2.5-flash-preview-tts",   # 저렴 (출력 $10/1M vs $20/1M)
    "gemini-2.5-pro-preview-tts",     # 최고 품질 주장
]

# Gemini 오디오 출력 규격 (문서 기준): 24kHz / 16bit / mono PCM
GEMINI_PCM = dict(rate=24000, width=2, channels=1)


# ---------------------------------------------------------------------------
# 공통 유틸
# ---------------------------------------------------------------------------
def save_pcm_as_wav(path: Path, pcm: bytes, rate=24000, width=2, channels=1):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(pcm)


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate()


def post_json(url: str, headers: dict, payload: dict, timeout=60):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=timeout)


# ---------------------------------------------------------------------------
# Gemini TTS
# ---------------------------------------------------------------------------
def _gemini_client():
    from google import genai

    # ⚠️ .strip() 을 빼면 안 된다. server/live.py 가 이 함수로 Live 클라이언트를 만드는데,
    #    .env 를 python-dotenv 로 읽는 로컬과 달리 **Docker --env-file 은 `=` 뒤의 공백을
    #    값에 그대로 포함시킨다.** 키에 공백이 하나 붙으면 대화만 인증에 실패하고,
    #    /healthz 의 api_key 는 config.py 가 strip 한 값을 보므로 true 로 나온다.
    #    "stylize 는 되는데 대화만 죽는" 상태라 엉뚱하게 Live 쪽을 의심하게 된다.
    key = (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("GEMINI_API_KEY 없음 (.env 확인)")
    return genai.Client(api_key=key)


def gemini_tts(text: str, voice: str, model: str, style: str = STYLE_DIRECTION):
    """한 번에 받기(non-streaming). returns (pcm_bytes, total_sec, usage)"""
    from google.genai import types

    client = _gemini_client()
    prompt = f"{style}\n\nNow say this line in Korean:\n{text}"

    t0 = time.perf_counter()
    resp = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)
                )
            ),
        ),
    )
    total = time.perf_counter() - t0

    # 간헐적으로 parts 가 비어서 옵니다(안전필터/빈응답). 호출부에서 재시도할 수 있게 명확히 알림.
    cand = resp.candidates[0] if resp.candidates else None
    parts = (cand.content.parts if cand and cand.content else None) or []
    part = next((p for p in parts if p.inline_data), None)
    if part is None:
        raise RuntimeError(f"오디오 없음 (finish_reason={cand.finish_reason if cand else 'no candidate'})")

    usage = {}
    if getattr(resp, "usage_metadata", None):
        u = resp.usage_metadata
        usage = {
            "prompt_tokens": u.prompt_token_count,
            "output_tokens": getattr(u, "candidates_token_count", None) or 0,
            "total_tokens": u.total_token_count,
        }
    return part.inline_data.data, total, usage


def gemini_tts_stream(text: str, voice: str, model: str, style: str = STYLE_DIRECTION):
    """스트리밍. 대화 모드에서 실제로 중요한 건 TTFB(첫 소리까지)입니다.
    returns (pcm_bytes, ttfb_sec, total_sec)"""
    from google.genai import types

    client = _gemini_client()
    prompt = f"{style}\n\nNow say this line in Korean:\n{text}"

    chunks, ttfb = [], None
    t0 = time.perf_counter()
    stream = client.models.generate_content_stream(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)
                )
            ),
        ),
    )
    for ev in stream:
        # 마지막 이벤트는 content/parts 가 비어서 오는 경우가 있음 (finish 신호)
        if not ev.candidates or not ev.candidates[0].content:
            continue
        for p in ev.candidates[0].content.parts or []:
            if p.inline_data and p.inline_data.data:
                if ttfb is None:
                    ttfb = time.perf_counter() - t0
                chunks.append(p.inline_data.data)
    total = time.perf_counter() - t0
    if not chunks:
        raise RuntimeError("스트리밍 오디오 없음")
    return b"".join(chunks), ttfb, total


# ---------------------------------------------------------------------------
# OpenAI TTS (gpt-4o-mini-tts) - REST 직접 호출
# ---------------------------------------------------------------------------
OPENAI_VOICES = ["coral", "shimmer", "nova"]


def openai_tts(text: str, voice: str = "coral", model: str = "gpt-4o-mini-tts"):
    """returns (wav_bytes, ttfb_sec, total_sec) - 응답 첫 바이트를 TTFB로 측정"""
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY 없음")

    t0 = time.perf_counter()
    resp = post_json(
        "https://api.openai.com/v1/audio/speech",
        {"Authorization": f"Bearer {key}"},
        {
            "model": model,
            "voice": voice,
            "input": text,
            "instructions": STYLE_DIRECTION,
            "response_format": "wav",
        },
    )
    first = resp.read(4096)
    ttfb = time.perf_counter() - t0
    body = first + resp.read()
    return body, ttfb, time.perf_counter() - t0


# ---------------------------------------------------------------------------
# ElevenLabs - REST 직접 호출
# ---------------------------------------------------------------------------
ELEVEN_MODELS = [
    ("eleven_flash_v2_5", "최저지연 ~75ms 주장 / 크레딧 0.5배 / 스타일 지시 불가"),
    ("eleven_multilingual_v2", "품질 우선 / 크레딧 1배 / 29개 언어"),
    ("eleven_v3", "표현력 최고 / 오디오 태그 지원 / 실시간 부적합"),
]

# ElevenLabs 는 자연어 스타일 지시를 못 받습니다. v3 만 대사 안에 오디오 태그를 넣을 수 있습니다.
ELEVEN_V3_TAGGED = "[excited] 안녕! [warmly] 오늘 나랑 같이 밥 먹을까?"


def eleven_list_voices(limit=10):
    key = os.environ.get("ELEVENLABS_API_KEY")
    req = urllib.request.Request(
        "https://api.elevenlabs.io/v1/voices", headers={"xi-api-key": key}
    )
    data = json.loads(urllib.request.urlopen(req, timeout=30).read())
    return [(v["voice_id"], v["name"], v.get("labels", {})) for v in data["voices"][:limit]]


def eleven_doll_score(labels: dict) -> int:
    """'아이의 인형 친구' 목소리로 적합한 정도. 높을수록 먼저 스크리닝한다.

    ElevenLabs 기본 프리셋은 성우/내레이터용 중년 남성이 앞쪽에 몰려 있어서,
    앞에서부터 N개를 그냥 뽑으면 이 서비스에 쓸 수 없는 목소리만 테스트하게 된다.
    """
    s = 0
    if labels.get("gender") == "female":
        s += 3
    if labels.get("age") == "young":
        s += 3
    if labels.get("age") in ("middle_aged", "old"):
        s -= 2
    if labels.get("use_case") == "characters_animation":
        s += 3
    if labels.get("descriptive") in ("cute", "sassy", "hyped", "upbeat", "chill"):
        s += 2
    if labels.get("descriptive") in ("professional", "formal", "classy", "mature", "crisp"):
        s -= 3
    if labels.get("use_case") in ("informative_educational", "advertisement", "narrative_story"):
        s -= 2  # 아나운서/내레이터 톤
    return s


def eleven_tts(text: str, voice_id: str, model_id: str = "eleven_flash_v2_5"):
    """스트리밍 엔드포인트로 TTFB 측정. pcm_24000 으로 받아 wav 로 저장.
    returns (pcm_bytes, ttfb_sec, total_sec)"""
    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        raise RuntimeError("ELEVENLABS_API_KEY 없음")

    url = (
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"
        "?output_format=pcm_24000"
    )
    t0 = time.perf_counter()
    resp = post_json(url, {"xi-api-key": key}, {"text": text, "model_id": model_id})
    first = resp.read(4096)
    ttfb = time.perf_counter() - t0
    body = first + resp.read()
    return body, ttfb, time.perf_counter() - t0


# ---------------------------------------------------------------------------
# 립싱크 실현성 검증
#   AI 는 입 모양 PNG 3장만 만든다. 언제 어느 장을 띄울지는 오디오 음량으로 정한다.
#   -> 이 함수가 그 타임라인을 실제로 뽑아본다. Android 에서 똑같이 하면 된다.
# ---------------------------------------------------------------------------
def lipsync_timeline(wav_path: Path, fps: int = 30):
    with wave.open(str(wav_path), "rb") as w:
        assert w.getsampwidth() == 2, "16bit PCM 만 지원"
        rate, ch = w.getframerate(), w.getnchannels()
        raw = w.readframes(w.getnframes())

    samples = struct.unpack(f"<{len(raw) // 2}h", raw)
    if ch == 2:
        samples = samples[::2]

    hop = rate // fps
    frames = []
    for i in range(0, len(samples) - hop, hop):
        window = samples[i : i + hop]
        rms = (sum(s * s for s in window) / len(window)) ** 0.5
        frames.append(rms)

    peak = max(frames) or 1.0
    norm = [f / peak for f in frames]

    def mouth(v):
        if v < 0.08:
            return "closed"
        if v < 0.30:
            return "half"
        return "open"

    timeline = [
        {"t": round(i / fps, 3), "level": round(v, 3), "mouth": mouth(v)}
        for i, v in enumerate(norm)
    ]
    return timeline, rate


# ---------------------------------------------------------------------------
# 1단계: voice 스크리닝
# ---------------------------------------------------------------------------
def run_voices(args):
    OUT.mkdir(parents=True, exist_ok=True)
    model = args.model or GEMINI_TTS_MODELS[0]
    text = LINES["greet"]
    log = []

    max_tier = 3 if args.all else args.tier
    targets = [(v, n, t) for v, n, t in GEMINI_VOICES if t <= max_tier]

    print(f"\n[1단계] Gemini voice 스크리닝  model={model}")
    print(f"  대사: {text}")
    print(f"  대상: tier<={max_tier}  {len(targets)}/{len(GEMINI_VOICES)}개  (약 {len(targets)*4.4:.0f}원)\n")

    for voice, note, tier in targets:
        path = OUT / f"voice_{voice}.wav"
        if path.exists() and not args.force:
            print(f"  [SKIP] {voice:<14} 이미 있음 (--force 로 재생성)")
            continue
        try:
            pcm, sec, usage = gemini_tts(text, voice, model)
            save_pcm_as_wav(path, pcm, **GEMINI_PCM)
            dur = wav_duration(path)
            print(f"  [OK]   {voice:<14} T{tier}  {sec:5.2f}s  오디오 {dur:4.1f}s  ({note})")
            log.append(
                {"voice": voice, "note": note, "tier": tier, "ok": True,
                 "gen_sec": round(sec, 2), "audio_sec": round(dur, 2),
                 "usage": usage, "file": path.name}
            )
        except Exception as e:
            print(f"  [FAIL] {voice:<14} {type(e).__name__}: {e}")
            log.append({"voice": voice, "ok": False, "error": f"{type(e).__name__}: {e}"})

    if log:
        (OUT / "report_voices.json").write_text(
            json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    print(f"\n결과: {OUT}")
    for t in (1, 2, 3):
        names = [v for v, _, tt in targets if tt == t]
        if names:
            print(f"\n  tier {t} 듣기:")
            print("    for v in " + " ".join(names) + "; do echo $v; afplay out/tts/voice_$v.wav; done")
    print("\n평가 기준:")
    print("  [ ] 인형 친구 목소리로 들리는가? (아나운서 톤이면 탈락)")
    print("  [ ] 4살 아이가 무서워하지 않을 톤인가?")
    print("  [ ] 성인이 아이 목소리를 흉내 내는 느낌이 아닌가?")


# ---------------------------------------------------------------------------
# 스타일 변주: voice 는 고정하고 연기 지시만 바꿔본다
#   -> "목소리 커스터마이징이 어디까지 되는가" 를 귀로 확인하는 용도
# ---------------------------------------------------------------------------
def run_styles(args):
    OUT.mkdir(parents=True, exist_ok=True)
    voice = args.voice or "Leda"
    model = args.model or GEMINI_TTS_MODELS[0]
    log = []

    print(f"\n[스타일 변주] voice={voice} 고정, 연기 지시만 교체  model={model}")
    print(f"  같은 성대로 어디까지 다르게 연기하는지 확인합니다. ({len(STYLE_VARIANTS)}개, 약 {len(STYLE_VARIANTS)*4.4:.0f}원)\n")

    # 상황에 맞는 대사를 골라 씁니다. 지시와 대사가 따로 놀면 어색하게 들립니다.
    line_for = {
        "none": "greet", "friend": "greet", "excited": "praise",
        "sleepy": "greet", "singsong": "sing", "whisper": "greet",
    }

    for name, style in STYLE_VARIANTS.items():
        text = LINES[line_for.get(name, "greet")]
        try:
            pcm, sec, usage = gemini_tts(text, voice, model, style=style)
            path = OUT / f"style_{name}.wav"
            save_pcm_as_wav(path, pcm, **GEMINI_PCM)
            dur = wav_duration(path)
            print(f"  [OK]   {name:<10} {sec:5.2f}s  오디오 {dur:4.1f}s   \"{text}\"")
            log.append({"style": name, "voice": voice, "line": text, "ok": True,
                        "gen_sec": round(sec, 2), "audio_sec": round(dur, 2),
                        "usage": usage, "file": path.name})
        except Exception as e:
            print(f"  [FAIL] {name:<10} {type(e).__name__}: {e}")
            log.append({"style": name, "ok": False, "error": f"{type(e).__name__}: {e}"})

    (OUT / "report_styles.json").write_text(
        json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n  듣기: for s in {' '.join(STYLE_VARIANTS)}; do echo $s; afplay out/tts/style_$s.wav; done")
    print("\n  확인할 것:")
    print("    [ ] none(지시 없음) 과 friend 가 실제로 다르게 들리는가?  <- 다르면 프롬프트가 먹힌다는 뜻")
    print("    [ ] excited / sleepy 가 구분되는가?  <- 앱에서 감정 전환이 가능한지의 근거")
    print("    [ ] 같은 인형 목소리로 유지되는가?  <- 캐릭터가 바뀌면 안 됨")


# ---------------------------------------------------------------------------
# 2단계: 엔진 비교
# ---------------------------------------------------------------------------
def run_engines(args):
    OUT.mkdir(parents=True, exist_ok=True)
    voice = args.voice or "Leda"
    log = []

    print(f"\n[2단계] 엔진 비교   Gemini voice={voice}")
    print("  TTFB = 첫 소리까지. 대화 모드에서 체감 지연을 좌우하는 값.\n")

    # --- Gemini: 모델별 ---
    for model in GEMINI_TTS_MODELS:
        for key, text in LINES.items():
            tag = f"gemini/{model.replace('-preview','').replace('gemini-','')}/{key}"
            try:
                pcm, sec, usage = gemini_tts(text, voice, model)
                path = OUT / f"eng_{model}_{key}.wav"
                save_pcm_as_wav(path, pcm, **GEMINI_PCM)
                dur = wav_duration(path)
                print(f"  [OK]   {tag:<44} 총 {sec:5.2f}s  오디오 {dur:4.1f}s")
                log.append({"engine": "gemini", "model": model, "line": key, "ok": True,
                            "total_sec": round(sec, 2), "audio_sec": round(dur, 2),
                            "usage": usage, "file": path.name})
            except Exception as e:
                print(f"  [FAIL] {tag:<44} {type(e).__name__}: {e}")
                log.append({"engine": "gemini", "model": model, "line": key,
                            "ok": False, "error": f"{type(e).__name__}: {e}"})

    # --- Gemini 스트리밍 TTFB (3.1 만 지원) ---
    print()
    for key, text in LINES.items():
        tag = f"gemini-stream/{key}"
        try:
            pcm, ttfb, total = gemini_tts_stream(text, voice, "gemini-3.1-flash-tts-preview")
            path = OUT / f"eng_stream_{key}.wav"
            save_pcm_as_wav(path, pcm, **GEMINI_PCM)
            print(f"  [OK]   {tag:<44} TTFB {ttfb:5.2f}s  총 {total:5.2f}s")
            log.append({"engine": "gemini-stream", "model": "gemini-3.1-flash-tts-preview",
                        "line": key, "ok": True, "ttfb_sec": round(ttfb, 2),
                        "total_sec": round(total, 2), "file": path.name})
        except Exception as e:
            print(f"  [FAIL] {tag:<44} {type(e).__name__}: {e}")
            log.append({"engine": "gemini-stream", "line": key, "ok": False,
                        "error": f"{type(e).__name__}: {e}"})

    # --- OpenAI ---
    print()
    if os.environ.get("OPENAI_API_KEY"):
        for ov in OPENAI_VOICES:
            key, text = "greet", LINES["greet"]
            tag = f"openai/gpt-4o-mini-tts/{ov}"
            try:
                data, ttfb, total = openai_tts(text, voice=ov)
                path = OUT / f"eng_openai_{ov}.wav"
                path.write_bytes(data)
                print(f"  [OK]   {tag:<44} TTFB {ttfb:5.2f}s  총 {total:5.2f}s")
                log.append({"engine": "openai", "voice": ov, "line": key, "ok": True,
                            "ttfb_sec": round(ttfb, 2), "total_sec": round(total, 2),
                            "file": path.name})
            except urllib.error.HTTPError as e:
                print(f"  [FAIL] {tag:<44} HTTP {e.code}: {e.read()[:200].decode(errors='replace')}")
                log.append({"engine": "openai", "voice": ov, "ok": False, "error": f"HTTP {e.code}"})
            except Exception as e:
                print(f"  [FAIL] {tag:<44} {type(e).__name__}: {e}")
                log.append({"engine": "openai", "voice": ov, "ok": False,
                            "error": f"{type(e).__name__}: {e}"})
    else:
        print("  [SKIP] OPENAI_API_KEY 없음")

    # --- ElevenLabs ---
    print()
    if os.environ.get("ELEVENLABS_API_KEY"):
        run_eleven(args, log)
    else:
        print("  [SKIP] ELEVENLABS_API_KEY 없음 (.env 에 추가하면 비교에 포함됩니다)")

    (OUT / "report_engines.json").write_text(
        json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n결과: {OUT}/report_engines.json")


# ---------------------------------------------------------------------------
# ElevenLabs 전용 (Gemini 재호출 없이 ElevenLabs 만 검증 - 비용 절약)
# ---------------------------------------------------------------------------
def run_eleven(args, log=None):
    OUT.mkdir(parents=True, exist_ok=True)
    standalone = log is None
    log = [] if standalone else log

    if not os.environ.get("ELEVENLABS_API_KEY"):
        print("  ELEVENLABS_API_KEY 없음. .env 에 넣고 다시 실행하세요.")
        return

    # --- 계정 voice 목록 ---
    try:
        voices = eleven_list_voices(limit=30)
    except urllib.error.HTTPError as e:
        print(f"  [FAIL] voice 목록 조회 HTTP {e.code}: {e.read()[:300].decode(errors='replace')}")
        return
    except Exception as e:
        print(f"  [FAIL] voice 목록 조회: {type(e).__name__}: {e}")
        return

    # 인형 친구 적합도 순으로 정렬 - 앞에서 N개만 실제로 합성한다
    ranked = sorted(voices, key=lambda v: -eleven_doll_score(v[2]))
    print(f"  ElevenLabs 계정 voice {len(voices)}개 (인형 적합도 순, 상위 {args.eleven_n}개만 합성):")
    for i, (vid, name, labels) in enumerate(ranked):
        mark = "→" if i < args.eleven_n else " "
        short = name.split(" - ")[0]
        desc = ", ".join(f"{k}={v}" for k, v in labels.items() if v and k != "language")
        print(f"   {mark} [{eleven_doll_score(labels):+d}] {short:<10} {desc}")

    fixed = args.voice or os.environ.get("ELEVENLABS_VOICE_ID") or ""
    targets = (
        [(fixed, fixed[:8])]
        if fixed
        else [(vid, name.split(" - ")[0]) for vid, name, _ in ranked[: args.eleven_n]]
    )

    # --- 1) voice 스크리닝: 한국어 발음이 되는 voice 를 먼저 걸러낸다 ---
    # ElevenLabs 프리셋은 대부분 영어 화자라, 한국어를 시키면 억양이 무너지는 경우가 있음.
    print(f"\n  [A] 한국어 발음 스크리닝  model=eleven_flash_v2_5  ({len(targets)}개 voice)")
    text = LINES["greet"]
    for vid, name in targets:
        tag = f"eleven/{name}"
        try:
            pcm, ttfb, total = eleven_tts(text, vid, "eleven_flash_v2_5")
            safe = "".join(c for c in name if c.isalnum() or c in "-_")
            path = OUT / f"eleven_voice_{safe}.wav"
            save_pcm_as_wav(path, pcm, rate=24000, width=2, channels=1)
            print(f"    [OK]   {tag:<30} TTFB {ttfb:5.2f}s  총 {total:5.2f}s  오디오 {wav_duration(path):4.1f}s")
            log.append({"engine": "elevenlabs", "stage": "voice-screen", "voice": name,
                        "voice_id": vid, "model": "eleven_flash_v2_5", "ok": True,
                        "ttfb_sec": round(ttfb, 2), "total_sec": round(total, 2),
                        "file": path.name})
        except urllib.error.HTTPError as e:
            body = e.read()[:300].decode(errors="replace")
            print(f"    [FAIL] {tag:<30} HTTP {e.code}: {body}")
            log.append({"engine": "elevenlabs", "stage": "voice-screen", "voice": name,
                        "ok": False, "error": f"HTTP {e.code} {body}"})
        except Exception as e:
            print(f"    [FAIL] {tag:<30} {type(e).__name__}: {e}")
            log.append({"engine": "elevenlabs", "stage": "voice-screen", "voice": name,
                        "ok": False, "error": f"{type(e).__name__}: {e}"})

    # --- 2) 모델 비교: 지연 vs 표현력 트레이드오프 확인 ---
    base_id, base_name = targets[0]
    print(f"\n  [B] 모델 비교  voice={base_name}")
    for model_id, note in ELEVEN_MODELS:
        body = ELEVEN_V3_TAGGED if model_id == "eleven_v3" else text
        tag = f"eleven/{model_id}"
        try:
            pcm, ttfb, total = eleven_tts(body, base_id, model_id)
            path = OUT / f"eleven_model_{model_id}.wav"
            save_pcm_as_wav(path, pcm, rate=24000, width=2, channels=1)
            print(f"    [OK]   {tag:<30} TTFB {ttfb:5.2f}s  총 {total:5.2f}s   ({note})")
            log.append({"engine": "elevenlabs", "stage": "model-compare", "model": model_id,
                        "voice": base_name, "ok": True, "ttfb_sec": round(ttfb, 2),
                        "total_sec": round(total, 2), "text": body, "file": path.name})
        except urllib.error.HTTPError as e:
            b = e.read()[:300].decode(errors="replace")
            print(f"    [FAIL] {tag:<30} HTTP {e.code}: {b}")
            log.append({"engine": "elevenlabs", "stage": "model-compare", "model": model_id,
                        "ok": False, "error": f"HTTP {e.code} {b}"})
        except Exception as e:
            print(f"    [FAIL] {tag:<30} {type(e).__name__}: {e}")
            log.append({"engine": "elevenlabs", "stage": "model-compare", "model": model_id,
                        "ok": False, "error": f"{type(e).__name__}: {e}"})

    if standalone:
        (OUT / "report_eleven.json").write_text(
            json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n  결과: {OUT}/report_eleven.json")
        print("  듣기: for f in out/tts/eleven_*.wav; do echo $f; afplay $f; done")
        print("\n  평가 기준:")
        print("    [ ] 한국어 억양이 영어 화자 티가 나지 않는가?  (여기서 대부분 갈립니다)")
        print("    [ ] Gemini Leda 대비 인형 목소리로 더 그럴듯한가?")
        print("    [ ] TTFB 가 Gemini 스트리밍 1.3초보다 유의미하게 빠른가?")


# ---------------------------------------------------------------------------
# 보조: Gemini 블라인드 청취 평가
#
# ⚠️ 이 기능은 순위를 못 냅니다. 2026-08-04 실측:
#      1차: Leda 20점 / Callirrhoe 14점
#      2차(3회 평균): Callirrhoe 19.0 / Leda 15.3   <- 완전히 뒤집힘
#      같은 파일 4회 반복 시 총점 편차 폭 최대 6점
#    - 쓸 수 있는 용도: 명백히 나쁜 후보(아나운서 톤 등)를 1차로 걸러내기
#    - 쓸 수 없는 용도: 상위 후보 순위 매기기 -> 반드시 사람이 귀로 판단할 것
#    그래서 반복 평균 + 편차 폭을 항상 같이 출력합니다.
# ---------------------------------------------------------------------------
JUDGE_RUBRIC = """이 오디오는 한국 4살 아이의 애착인형이 말을 거는 장면에 쓸 음성 후보입니다.
대사: {line}

한국어 원어민 관점에서 아래 4항목을 1~5점 정수로 냉정하게 평가하세요. 후하게 주지 마세요.
- native: 한국어 원어민처럼 들리는가 (외국인 억양/어색한 강세가 있으면 감점)
- prosody: 문장 억양이 자연스러운가
- child_friendly: 4살 아이에게 다정한 인형 친구 목소리인가 (아나운서/성인 성우 톤이면 감점)
- toylike: 작고 귀여운 봉제인형이 말하는 것처럼 들리는가

JSON만 출력: {{"native":n,"prosody":n,"child_friendly":n,"toylike":n}}"""


def judge_audio(path: Path, line: str, model="gemini-3.5-flash"):
    import re

    from google.genai import types

    client = _gemini_client()
    resp = client.models.generate_content(
        model=model,
        contents=[
            types.Part.from_bytes(data=path.read_bytes(), mime_type="audio/wav"),
            JUDGE_RUBRIC.format(line=line),
        ],
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    nums = [float(x) for x in re.findall(r":\s*([\d.]+)", resp.text or "")][:4]
    return sum(nums) if len(nums) == 4 else None


def run_judge(args):
    import statistics

    files = sorted(OUT.glob("voice_*.wav")) + sorted(OUT.glob("eleven_voice_*.wav"))
    if not files:
        print("평가할 wav 가 없습니다. --voices / --eleven 를 먼저 실행하세요.")
        return

    n = args.judge_n
    print(f"\n[보조] 블라인드 청취 평가  ({len(files)}개 × {n}회 반복)")
    print("  ⚠️ 편차 폭이 3 이상이면 그 점수로 순위를 매기지 마세요. 사람이 들어야 합니다.\n")

    rows = []
    for f in files:
        scores = [s for _ in range(n) if (s := judge_audio(f, LINES["greet"])) is not None]
        if not scores:
            print(f"  [FAIL] {f.stem}")
            continue
        mean, spread = statistics.mean(scores), max(scores) - min(scores)
        rows.append({"file": f.stem, "mean": round(mean, 1), "spread": spread, "scores": scores})

    rows.sort(key=lambda r: -r["mean"])
    print(f"  {'voice':<32}{'평균':>6}{'편차폭':>7}   개별")
    for r in rows:
        flag = "  ← 편차 큼, 신뢰 불가" if r["spread"] >= 3 else ""
        print(f"  {r['file']:<32}{r['mean']:>6.1f}{r['spread']:>7.0f}   {r['scores']}{flag}")

    (OUT / "report_judge.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    top = rows[0]["mean"] if rows else 0
    close = [r["file"] for r in rows if top - r["mean"] < 1.5]
    if len(close) > 1:
        print(f"\n  1.5점 이내 동률권 {len(close)}개 -> 이 중에서는 귀로 고르세요:")
        for c in close:
            print(f"    afplay {OUT.name}/{c}.wav")


# ---------------------------------------------------------------------------
# 3단계: 립싱크
# ---------------------------------------------------------------------------
def run_lipsync(args):
    path = Path(args.lipsync)
    if not path.exists():
        print(f"파일 없음: {path}")
        sys.exit(1)

    timeline, rate = lipsync_timeline(path, fps=args.fps)
    out_json = path.with_suffix(".lipsync.json")
    out_json.write_text(json.dumps(timeline, ensure_ascii=False), encoding="utf-8")

    counts = {"closed": 0, "half": 0, "open": 0}
    for f in timeline:
        counts[f["mouth"]] += 1

    print(f"\n[3단계] 립싱크 타임라인   {path.name}  ({rate}Hz, {args.fps}fps)")
    print(f"  프레임 {len(timeline)}개 = {len(timeline)/args.fps:.1f}초")
    print(f"  입 모양 분포: closed {counts['closed']}  half {counts['half']}  open {counts['open']}")
    print("\n  앞 2초 미리보기:")
    for f in timeline[: args.fps * 2]:
        bar = "#" * int(f["level"] * 30)
        print(f"    {f['t']:5.2f}s  {f['mouth']:<7} |{bar}")
    print(f"\n  -> {out_json.name}")
    print("\n  판단 기준:")
    print("    - closed 만 잔뜩 나오면 임계값(0.08/0.30)을 낮춰야 함")
    print("    - 세 종류가 고르게 섞여 나오면 Android 에서 그대로 구현 가능")
    print("    - Android 는 이 JSON 을 쓰지 않고 오디오 재생 중 실시간 RMS 로 같은 계산을 한다")


# ---------------------------------------------------------------------------
def main():
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--voices", action="store_true", help="1단계: Gemini voice 스크리닝")
    ap.add_argument("--engines", action="store_true", help="2단계: 엔진/모델 비교")
    ap.add_argument("--eleven", action="store_true", help="ElevenLabs 만 검증 (Gemini 재호출 없음)")
    ap.add_argument("--styles", action="store_true", help="voice 고정하고 연기 지시만 바꿔 비교")
    ap.add_argument("--all", action="store_true", help="voice 30개 전부 생성 (기본은 tier1~2)")
    ap.add_argument("--tier", type=int, default=2, help="생성할 최대 tier (기본 2)")
    ap.add_argument("--force", action="store_true", help="이미 있는 wav 도 다시 생성")
    ap.add_argument("--judge", action="store_true", help="보조: 생성된 wav 블라인드 청취 평가")
    ap.add_argument("--judge-n", type=int, default=3, help="평가 반복 횟수 (기본 3)")
    ap.add_argument("--lipsync", metavar="WAV", help="3단계: wav 립싱크 타임라인 추출")
    ap.add_argument("--voice", help="Gemini voice 이름 또는 ElevenLabs voice_id")
    ap.add_argument("--model", help="voice 스크리닝에 쓸 모델")
    ap.add_argument("--eleven-n", type=int, default=6, help="ElevenLabs 스크리닝할 voice 개수")
    ap.add_argument("--fps", type=int, default=30, help="립싱크 프레임레이트 (기본 30)")
    args = ap.parse_args()

    if args.voices:
        run_voices(args)
    elif args.styles:
        run_styles(args)
    elif args.engines:
        run_engines(args)
    elif args.eleven:
        print("\n[ElevenLabs 단독 검증]")
        run_eleven(args)
    elif args.judge:
        run_judge(args)
    elif args.lipsync:
        run_lipsync(args)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
