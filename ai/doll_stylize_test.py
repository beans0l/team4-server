"""
인형 사진 -> 유아틱 2D 캐릭터 변환 비교 테스트

사용법:
    # 1) 프로젝트 루트에 .env 파일 만들고 키 넣기
    #    GEMINI_API_KEY=AIza...
    # 2) 실행
    ../.venv/bin/python doll_stylize_test.py 인형사진.jpg

    # 사용 가능한 모델 확인만 하고 싶을 때
    ../.venv/bin/python doll_stylize_test.py --list-models

결과: out/ 폴더에 단계별 PNG + report.json (소요시간 기록)
"""

import argparse
import io
import json
import os
import sys
import threading
import time
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "out"


# ---------------------------------------------------------------------------
# .env 로더 (python-dotenv 의존성 없이)
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
            key = key.strip()
            # `export KEY=val` 도 받는다. 이걸 안 벗기면 키 이름이
            # "export GEMINI_API_KEY" 가 되어 환경변수가 설정되지 않고,
            # .env 에 키가 멀쩡히 보이는데 "키 없음" 오류가 난다.
            if key.startswith("export "):
                key = key[len("export ") :].strip()
            if not key:
                continue
            val = val.strip().strip("\"'")
            # 실제 환경변수가 이긴다(docker run -e 주입이 .env 파일보다 우선).
            # 다만 조용히 지면 "'.env' 를 고쳤는데 왜 그대로지?" 로 한참 헤매게 되므로
            # 가려졌다는 사실만 알린다.
            if key in os.environ and os.environ[key] != val:
                print(
                    f"  [주의] 셸의 {key} 가 .env 값을 가립니다 (셸 값을 사용).",
                    file=sys.stderr,
                )
            os.environ.setdefault(key, val)


# ---------------------------------------------------------------------------
# 프롬프트 - 품질의 8할입니다. 결과 보면서 계속 튜닝하세요.
# ---------------------------------------------------------------------------
STYLE_PROMPT = """\
Convert this photo of a plush toy into a 2D children's picture-book illustration.

CRITICAL - keep the exact same character:
- Same colors, same proportions, same distinctive features (ears, patterns, accessories)
- A child must instantly recognize it as their own toy

Style:
- Soft rounded shapes, thick clean outlines, flat pastel shading
- Cute and friendly, suitable for a 3-6 year old
- No photo texture, no fabric fuzz, no realistic lighting

Ignore and remove these - they are manufacturing artifacts, not part of the character:
- Care labels, brand tags, sewn-in fabric tags, barcodes, any printed text
- Dust, stains, seam lines, price stickers

Composition:
- Full body, front-facing, centered, standing pose
- Plain solid white background
- No shadow, no props, no text, no watermark
"""

# v3 - v1 기반 + 최소한의 캐릭터화. "재설계 금지"를 명시해 v2의 실패(정체성 상실)를 차단.
PROMPT_V3 = """\
Convert this photo of a plush toy into a 2D children's picture-book illustration.

CRITICAL - keep the exact same character:
- Same colors, same proportions, same distinctive features (ears, patterns, accessories)
- A child must instantly recognize it as their own toy

Add ONLY these two things to make it feel alive:
- A small friendly smiling mouth
- Small white highlights in the existing eyes

Keep everything else EXACTLY as in the photo:
- Same body shape, same proportions, same silhouette
- Same flippers/limbs - do NOT turn them into arms, hands, or humanlike legs
- Same shell/body pattern - do NOT invent a new pattern
- Do NOT redesign the character
- Do NOT turn it into a generic cartoon mascot
- Do NOT change the head size or make it chibi

Style:
- Soft rounded shapes, thick clean outlines, flat pastel shading
- Cute and friendly, suitable for a 3-6 year old
- No photo texture, no fabric fuzz, no realistic lighting

Ignore and remove these - they are manufacturing artifacts, not part of the character:
- Care labels, brand tags, sewn-in fabric tags, barcodes, any printed text
- Dust, stains, seam lines, price stickers

Composition:
- Full body, centered, plain solid white background
- No shadow, no props, no text, no watermark
"""

# v2 - 캐릭터화 강화안(실패: 정체성 상실). 결과물: ai/out/02_flash_v2.png
# 큰 눈 + 웃는 입 + 치비 비율 + "서 있는 자세" 강제. 쓰려면 STYLE_PROMPT 를 이걸로 교체.
PROMPT_V2 = """\
Convert this photo of a plush toy into a 2D children's picture-book illustration.

CRITICAL - keep the exact same character:
- Same colors, same proportions, same distinctive features (patterns, shell, fins, accessories)
- A child must instantly recognize it as their own toy
- Keep the shell/body pattern crisp and clearly defined, not blurred away

Bring it to life as a friendly character:
- Big round friendly eyes with white highlights
- A small smiling mouth
- Slightly chibi proportions (a little oversized head)
- It should look happy and approachable, like a cartoon friend for a 3-6 year old

Pose - IMPORTANT:
- Redraw the character as if it is ALIVE and STANDING UPRIGHT on its hind legs
- Facing the viewer straight on, at eye level
- NOT lying flat, NOT seen from above, NOT a top-down view

Style:
- Soft rounded shapes, thick clean outlines, flat pastel shading
- No photo texture, no fabric fuzz, no realistic lighting

Ignore and remove these - they are manufacturing artifacts, not part of the character:
- Care labels, brand tags, sewn-in fabric tags, barcodes, any printed text
- Dust, stains, seam lines, price stickers

Composition:
- Full body, centered, plain solid white background
- No shadow, no props, no text, no watermark
"""

# 모션용 포즈 시트 (기획서: 통통 튀기 / 반 접히기 / 오뚝이)
POSE_PROMPTS = {
    "jump": "Same character, same illustration style. Now in a joyful mid-air jumping pose with arms up. Plain solid white background, no shadow.",
    "bow": "Same character, same illustration style. Now bent forward at the waist, like folding in half. Plain solid white background, no shadow.",
    "tilt": "Same character, same illustration style. Now tilted 20 degrees to one side, like a roly-poly toy. Plain solid white background, no shadow.",
}

# Flash 만 기본으로 돈다. Pro 는 이미 비교가 끝나서 **탈락한** 모델이다
# (색이 파스텔로 밝아지고 없는 이목구비를 그려 넣는다). 그런데 여기 그냥 두면
# 프롬프트 튜닝하려고 스크립트를 한 번 돌릴 때마다 쓰지도 않을 Pro 에 200원씩
# 나간다. 크레딧이 $10 뿐이라 그 낭비가 시연을 위협한다.
# 다시 비교하고 싶을 때만 --pro 로 켠다.
FLASH_MODEL = "gemini-3.1-flash-image"
PRO_MODEL = "gemini-3-pro-image"
GEMINI_MODELS = [FLASH_MODEL]


# ---------------------------------------------------------------------------
# ★ 채택 프롬프트 — 이 스크립트와 서버가 함께 보는 단 하나의 출처.
#
# 버전을 바꿀 때는 여기 한 줄만 바꾼다. 서버(server/prompts.py)도 이 이름을
# import 하므로 양쪽이 자동으로 같이 움직인다.
#
# 예전에 이게 갈라져 있었다: 스크립트는 STYLE_PROMPT(v1) 를 실행하는데 서버는
# PROMPT_V3 를 쓰고 있었고, PROMPT_V3 는 스크립트에서 한 번도 호출되지 않는
# 죽은 코드였다. 그 상태로 프롬프트를 튜닝하면 스크립트로 확인한 결과와 앱에
# 나가는 결과가 다르다. 프롬프트는 두 곳에 두지 말 것.
# ---------------------------------------------------------------------------
ACTIVE_PROMPT = PROMPT_V3


# ---------------------------------------------------------------------------
# 배경 제거
# ---------------------------------------------------------------------------
# 서버 기본값. 모델별 실측(거북이 인형, 768x1024, M-series Mac CPU):
#
#   모델                     추론        실루엣 IoU   판정
#   isnet-general-use        1.2초       99.3%       🟢 채택
#   u2net                    0.5초       78.2%       🔴 손이 남는다
#   birefnet-general-lite    15~24초     78.0%       🔴 손이 남는다
#   birefnet-general         32~49초     기준        느리다
#
# birefnet-general 이 품질 기준이었으나 30~40배 느리다. isnet 은 알파 마스크가
# 99.3% 일치하면서 1.2초다. u2net 계열은 빠르지만 인형을 든 손을 제거하지 못해
# 탈락했다(초과 영역 27%). 손이 남으면 캐릭터 일관성이 무너진다.
#
# ⚠️ 거북이 인형 하나로만 비교했다(R2 과적합). 다른 인형에서도 손이 제거되는지
#    확인할 것.
DEFAULT_BG_MODEL = "isnet-general-use"

_bg_sessions: dict = {}
# 서버는 이 함수를 여러 스레드에서 동시에 부른다. 락이 없으면 두 스레드가 동시에
# 캐시 미스를 보고 각자 onnxruntime 세션을 만든다 — 2Gi 인스턴스에서 모델 메모리가
# 두 배로 뛰고, 그중 하나는 그냥 버려진다.
_bg_lock = threading.Lock()


def remove_bg(img: Image.Image, model: str = DEFAULT_BG_MODEL) -> Image.Image:
    from rembg import new_session, remove

    session = _bg_sessions.get(model)
    if session is None:
        with _bg_lock:
            session = _bg_sessions.get(model)  # 락 안에서 다시 확인
            if session is None:
                session = _bg_sessions[model] = new_session(model)
    return remove(img, session=session)


def flatten_on_white(img: Image.Image) -> Image.Image:
    """투명 PNG를 AI 입력용 흰 배경 RGB로 합성.
    알파를 그대로 넣으면 모델이 검은 배경으로 해석하는 경우가 있음."""
    if img.mode != "RGBA":
        return img.convert("RGB")
    white = Image.new("RGB", img.size, (255, 255, 255))
    white.paste(img, mask=img.split()[-1])
    return white


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------
def _gemini_client():
    from google import genai

    # .strip() 이 중요하다. 키를 파일이나 에디터에서 붙여 넣으면 끝에 개행·공백이
    # 딸려오기 쉽다. 안 벗기면 genai.Client 가 잘못된 HTTP 헤더로 실패하는데,
    # 서버 쪽 키 존재 검사는 통과한 뒤라 "키는 있는데 생성이 실패함"으로 오진단된다.
    key = (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("GEMINI_API_KEY 없음 (.env 확인)")
    return genai.Client(api_key=key)


def list_models():
    client = _gemini_client()
    print("\n계정에서 사용 가능한 이미지 관련 모델:\n")
    for m in client.models.list():
        name = m.name.replace("models/", "")
        if "image" in name or "imagen" in name:
            print(f"  {name}")


def run_gemini(src: Image.Image, prompt: str, model: str) -> Image.Image:
    from google.genai import types

    client = _gemini_client()
    buf = io.BytesIO()
    src.save(buf, format="PNG")

    resp = client.models.generate_content(
        model=model,
        contents=[
            types.Part.from_bytes(data=buf.getvalue(), mime_type="image/png"),
            prompt,
        ],
    )

    # 아래 단계별 확인은 전부 "왜 이미지가 없는지"를 남기기 위한 것이다.
    # 예전에는 resp.candidates[0] 를 바로 꺼내서, 안전 필터에 걸리면 candidates 가
    # 비어 오고 TypeError 가 났다. 그러면 정작 이유를 담은 아래 RuntimeError 에
    # 도달하지 못해서 로그에 원인이 안 남는다. 서버는 이걸 3회 재시도한 뒤
    # "이미지 생성에 실패했어요" 만 돌려주므로 아무도 원인을 모르게 된다.
    block = getattr(getattr(resp, "prompt_feedback", None), "block_reason", None)
    if block:
        raise RuntimeError(f"프롬프트가 차단됨 (block_reason={block})")

    if not resp.candidates:
        raise RuntimeError("응답에 candidates 가 없음")

    cand = resp.candidates[0]
    if cand.content is None or not cand.content.parts:
        raise RuntimeError(f"응답 parts 가 비어 있음 (finish_reason={cand.finish_reason})")

    for part in cand.content.parts:
        if part.inline_data:
            img = Image.open(io.BytesIO(part.inline_data.data))
            # Image.open 은 헤더만 읽는다. load() 로 여기서 끝까지 디코딩해야
            # 잘린 응답이 이 함수 안에서 터진다. 안 하면 한참 뒤 cutout() 에서
            # 터지는데, 거기는 재시도 범위 밖이라 재시도로 살릴 수 있는 실패가
            # 그대로 500 이 된다.
            img.load()
            return img
    raise RuntimeError(f"이미지 없음 (finish_reason={cand.finish_reason})")


# ---------------------------------------------------------------------------
# OpenAI (백업 후보 - 키 있을 때만 실행)
# ---------------------------------------------------------------------------
def run_openai(src: Image.Image, prompt: str, model: str = "gpt-image-2") -> Image.Image:
    import base64

    from openai import OpenAI

    client = OpenAI()
    buf = io.BytesIO()
    src.save(buf, format="PNG")
    buf.name = "doll.png"
    buf.seek(0)

    resp = client.images.edit(
        model=model,
        image=buf,
        prompt=prompt.replace("Plain solid white background", "Transparent background"),
        background="transparent",
        size="1024x1024",
    )
    return Image.open(io.BytesIO(base64.b64decode(resp.data[0].b64_json)))


# ---------------------------------------------------------------------------
def timed(log: list, label: str, fn, *args, **kwargs):
    t0 = time.perf_counter()
    try:
        result = fn(*args, **kwargs)
        dt = time.perf_counter() - t0
        print(f"  [OK]   {label:<32} {dt:6.2f}s")
        log.append({"step": label, "ok": True, "seconds": round(dt, 2)})
        return result
    except Exception as e:
        dt = time.perf_counter() - t0
        print(f"  [FAIL] {label:<32} {dt:6.2f}s  {type(e).__name__}: {e}")
        log.append({"step": label, "ok": False, "seconds": round(dt, 2), "error": f"{type(e).__name__}: {e}"})
        return None


def main():
    load_env()

    ap = argparse.ArgumentParser()
    ap.add_argument("image", nargs="?", help="인형 사진 경로")
    ap.add_argument("--list-models", action="store_true", help="사용 가능한 모델만 출력")
    ap.add_argument(
        "--pro",
        action="store_true",
        help=f"{PRO_MODEL} 도 함께 호출해 비교 (탈락한 모델이고 호출당 약 200원)",
    )
    args = ap.parse_args()

    models = GEMINI_MODELS + ([PRO_MODEL] if args.pro else [])

    if args.list_models:
        list_models()
        return
    if not args.image:
        ap.error("인형 사진 경로가 필요합니다")

    OUT.mkdir(exist_ok=True)
    log: list = []

    src_path = Path(args.image)
    original = Image.open(src_path).convert("RGB")
    print(f"\n입력: {src_path.name}  {original.size[0]}x{original.size[1]}")
    if min(original.size) < 768:
        print("  ⚠️  해상도가 낮습니다. 1024px 이상 권장")

    # 원본이 크면 축소 - rembg 속도 + API 업로드 비용 절감
    MAX_EDGE = 1536
    if max(original.size) > MAX_EDGE:
        ratio = MAX_EDGE / max(original.size)
        new_size = (round(original.size[0] * ratio), round(original.size[1] * ratio))
        original = original.resize(new_size, Image.LANCZOS)
        print(f"  → 리사이즈: {new_size[0]}x{new_size[1]}")

    # [1] 원본 누끼 -> 배경 오염 방지
    print("\nSTEP 1. 원본 배경 제거")
    cut = timed(log, f"rembg ({DEFAULT_BG_MODEL})", remove_bg, original)
    if cut is None:
        ai_input = original
    else:
        cut.save(OUT / "01_cutout.png")
        ai_input = flatten_on_white(cut)
        ai_input.save(OUT / "01_cutout_white.png")

    # [2] 스타일 변환
    print("\nSTEP 2. 스타일 변환")
    results: dict = {}
    for model in models:
        img = timed(log, model, run_gemini, ai_input, ACTIVE_PROMPT, model)
        if img:
            img.save(OUT / f"02_{model}.png")
            results[model] = img

    if os.environ.get("OPENAI_API_KEY"):
        img = timed(log, "gpt-image-2", run_openai, ai_input, ACTIVE_PROMPT)
        if img:
            img.save(OUT / "02_gpt-image-2.png")
            results["gpt-image-2"] = img
    else:
        print("  [SKIP] OPENAI_API_KEY 없음 - Gemini만 비교")

    if not results:
        print("\n❌ 스타일 변환 전부 실패. 위 에러 메시지 확인 필요.")
        (OUT / "report.json").write_text(json.dumps(log, ensure_ascii=False, indent=2))
        sys.exit(1)

    # [3] 결과물 누끼 -> AR 빌보드용 투명 PNG
    print("\nSTEP 3. 결과물 배경 제거 (AR 빌보드용)")
    for name, img in results.items():
        out = timed(log, f"rembg <- {name}", remove_bg, img)
        if out:
            out.save(OUT / f"03_{name}_alpha.png")

    # [4] 포즈 시트 - 캐릭터 일관성 검증
    base_model = FLASH_MODEL
    if base_model in results:
        print("\nSTEP 4. 모션용 포즈 생성 (캐릭터 일관성 검증)")
        base = results[base_model]
        for pose, prompt in POSE_PROMPTS.items():
            img = timed(log, f"pose: {pose}", run_gemini, base, prompt, base_model)
            if img:
                img.save(OUT / f"04_pose_{pose}.png")
                alpha = remove_bg(img)
                alpha.save(OUT / f"04_pose_{pose}_alpha.png")

    (OUT / "report.json").write_text(json.dumps(log, ensure_ascii=False, indent=2))

    total = sum(e["seconds"] for e in log)
    print(f"\n{'='*52}")
    print(f"완료 -> {OUT}")
    print(f"총 소요: {total:.1f}s")
    print(f"{'='*52}")
    print("\n평가 체크리스트:")
    print("  [ ] 아이가 자기 인형이라고 알아볼 수 있는가?  (정체성)")
    print("  [ ] 그림체가 유아 친화적인가?")
    print("  [ ] 누끼 경계가 깔끔한가?  (털/실루엣)")
    print("  [ ] 포즈 4장의 캐릭터가 서로 동일한가?  (일관성)")


if __name__ == "__main__":
    main()
