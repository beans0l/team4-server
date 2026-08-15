"""R1 검증 — 스프라이트 세트의 캐릭터 일관성 측정.

무엇을 확인하는가:
    립싱크는 입 3장을 초당 20~30회 교체한다. 그 사이 등껍질 무늬·색·자세가 조금이라도
    달라지면 정지 그림으로는 안 보이던 차이가 **캐릭터가 덜덜 떠는 것**으로 나타난다.
    프롬프트 v2 에서 모델이 캐릭터를 통째로 재설계한 전례가 있으므로 실제로 일어날 수 있다.

생성 전략 — 체이닝:
    원본에서 5장을 각각 독립 생성하지 않는다. base 를 먼저 만들고 **그 결과 이미지를
    입력으로** 변형을 요청한다. 모델이 원본 사진을 매번 새로 해석하면 자세부터 달라지는데
    (사전 누끼를 못 빼는 이유와 같은 문제), 이미 정리된 일러스트를 주면 해석의 여지가 없다.
    실제 파이프라인과도 맞는다 — base 는 stylize 가 이미 만들어 둔 것이다.

        원본 → rembg → 흰배경 → [Gemini] → base ─┬→ [Gemini] mouth_half
                                                  ├→ [Gemini] mouth_open
                                                  ├→ [Gemini] happy
                                                  └→ [Gemini] sleepy

측정 지표:
    iou_raw      캔버스만 맞추고 겹친 실루엣 IoU. **그대로 교체해도 되는가**
    iou_aligned  bbox 로 위치·크기를 정규화한 뒤 IoU. **형태 자체가 같은가**
    dcolor       공통 영역의 평균 RGB 거리. 몸 색이 유지되는가
    diff_band    차이가 세로 어디에 몰렸는가(상/중/하). 입 변형인데 전신이 바뀌면 재설계다

    iou_raw 가 낮고 iou_aligned 가 높으면 형태는 같고 위치만 밀린 것이라
    Android 에서 bbox 정렬로 복구할 수 있다. 둘 다 낮으면 방법 자체가 실패다.

사용법:
    .venv/bin/python -m ai.sprite_test --selftest      # 호출 없이 지표 코드만 점검
    .venv/bin/python -m ai.sprite_test doll.jpg        # 실제 생성 (Gemini 5회, 약 325원)
    .venv/bin/python -m ai.sprite_test --measure       # 기존 out/sprites/ 재측정 (무료)
    .venv/bin/python -m ai.sprite_test --recut         # 누끼만 다시 (무료). 파라미터 튜닝용
"""

import json
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.ndimage import label

from ai.cutout import cutout
from ai.doll_stylize_test import (
    ACTIVE_PROMPT,
    FLASH_MODEL,
    flatten_on_white,
    load_env,
    remove_bg,
    run_gemini,
)

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "out" / "sprites"

# ---------------------------------------------------------------------------
# 변형 프롬프트 — base 일러스트를 입력으로 받는다.
#
# 공통 접두사에 "바꾸지 말 것" 목록을 길게 넣은 이유: v2 의 교훈대로 **명시하지 않은
# 것은 지켜지지 않는다.** "입을 벌려줘"만 쓰면 모델이 그 김에 눈도 키우고 자세도 바꾼다.
# ---------------------------------------------------------------------------
_KEEP = """\
This is an existing character illustration. Keep it EXACTLY as it is and change only what I ask.

Keep IDENTICAL - this is critical:
- Same character, same colors, same shell/body pattern, same outline thickness
- Same pose, same body shape, same silhouette
- Same size, same position, same framing within the image
- Same eyes (unless I explicitly ask to change them)
- Do NOT redraw, do NOT redesign, do NOT restyle, do NOT re-center, do NOT zoom

Plain solid white background, no shadow, no text, no watermark.

Change ONLY this:
"""

SPRITE_PROMPTS = {
    "mouth_half": _KEEP + "- The mouth is slightly open, a small soft oval, as if saying a quiet syllable.",
    "mouth_open": _KEEP + "- The mouth is wide open in a round happy shape, as if singing out loud.",
    "happy": _KEEP + "- The eyes become happy upward-curved crescents and the mouth becomes a wide cheerful smile.",
    "sleepy": _KEEP + "- The eyes become closed sleepy curved lines and the mouth becomes a small relaxed curve.",
}

# ⚠️ 기본 세트에서 뺀 것. 서버는 SPRITE_BACK=1 일 때만 이걸 합친다.
#
# **R1 지표로 검증할 수 없다.** 위 4장은 "base 와 실루엣이 같아야" 통과인데(정렬 IoU
# 99.9%), 뒷모습은 실루엣이 달라야 정상이라 같은 잣대를 댈 수 없다. 즉 이건
# 나머지와 달리 **눈으로만 판정 가능한 미검증 스프라이트**다.
#
# 없어도 된다 — 2D 라 뒷면이 원래 없고, 한 바퀴 도는 동작은 Z축 제자리 스핀으로
# 대체된다. 켜면 등록 비용이 65원 더 든다.
OPTIONAL_SPRITE_PROMPTS = {
    "back": _KEEP
    + "- Show the SAME character from behind (back view). Keep the same colors, "
    "same pattern, same outline thickness, same size and framing. "
    "The face is not visible from this angle.",
}

# base 는 서버 파이프라인과 같은 프롬프트를 쓴다. 여기서 따로 두면 검증한 것과
# 서버가 내보내는 것이 갈라진다(prompts.py 가 경고하는 그 함정).
BASE_NAME = "mouth_closed"


# ---------------------------------------------------------------------------
# 측정
# ---------------------------------------------------------------------------
CANVAS = 512


def _mask(img: Image.Image) -> np.ndarray:
    return np.array(img.convert("RGBA").split()[-1]) > 128


def _main_blob(mask: np.ndarray) -> np.ndarray:
    """가장 큰 연결요소만 남긴다 = 캐릭터 본체.

    ⚠️ 이게 없으면 지표가 통째로 거짓말을 한다. 실측에서 변형 결과의 **맨 아랫줄**에
    82px 짜리 티끌이 몇 개 남았는데(누끼가 못 지운 비흰색 픽셀), 그것만으로 bbox 세로가
    569 → 936 으로 늘어나 정렬 IoU 가 99% 대신 37.9% 로 나왔다. 눈으로 보면 다섯 장이
    똑같은데 숫자만 FAIL 이었다.
    """
    lab, n = label(mask)
    if n == 0:
        return mask
    sizes = np.bincount(lab.ravel())
    sizes[0] = 0
    return lab == int(sizes.argmax())


def _strays(mask: np.ndarray) -> tuple[int, int]:
    """(본체 외 조각 수, 그 총 픽셀). 앱에서는 AR 화면의 티끌로 보인다."""
    lab, n = label(mask)
    if n <= 1:
        return 0, 0
    sizes = np.bincount(lab.ravel())
    sizes[0] = 0
    return int(n - 1), int(sizes.sum() - sizes.max())


def _bbox(mask: np.ndarray):
    rows = np.flatnonzero(mask.any(axis=1))
    cols = np.flatnonzero(mask.any(axis=0))
    if not rows.size or not cols.size:
        return None
    return int(cols[0]), int(rows[0]), int(cols[-1]) + 1, int(rows[-1]) + 1


def _fit(img: Image.Image, size: int = CANVAS) -> Image.Image:
    """캔버스 크기만 통일. 위치·배율은 건드리지 않는다 (iou_raw 용)."""
    return img.convert("RGBA").resize((size, size), Image.LANCZOS)


def _align(img: Image.Image, size: int = CANVAS, margin: int = 32) -> Image.Image:
    """bbox 를 캔버스 중앙에 맞춘다 — 위치·배율 차이를 제거 (iou_aligned 용).

    가로세로 비율은 유지한다. 늘려서 맞추면 '형태가 같은지'를 못 본다.
    """
    rgba = img.convert("RGBA")
    box = _bbox(_main_blob(_mask(rgba)))
    if box is None:
        return Image.new("RGBA", (size, size))
    crop = rgba.crop(box)
    inner = size - margin * 2
    scale = inner / max(crop.size)
    new = (max(1, round(crop.width * scale)), max(1, round(crop.height * scale)))
    crop = crop.resize(new, Image.LANCZOS)
    canvas = Image.new("RGBA", (size, size))
    canvas.paste(crop, ((size - new[0]) // 2, (size - new[1]) // 2))
    return canvas


def _iou(a: np.ndarray, b: np.ndarray) -> float:
    union = (a | b).sum()
    return float((a & b).sum() / union * 100) if union else 0.0


def compare(base: Image.Image, variant: Image.Image) -> dict:
    m_base_raw, m_var_raw = _mask(_fit(base)), _mask(_fit(variant))
    a, b = _align(base), _align(variant)
    m_base, m_var = _mask(a), _mask(b)

    # 색: 두 실루엣이 모두 불투명한 곳만 본다. 가장자리 차이가 섞이면 색 문제로 오독된다.
    both = m_base & m_var
    rgb_a = np.array(a.convert("RGB")).astype(np.float32)
    rgb_b = np.array(b.convert("RGB")).astype(np.float32)
    d = np.linalg.norm(rgb_a - rgb_b, axis=2)
    shared = d[both] if both.any() else np.array([0.0])

    # ⚠️ 평균만 보면 안 된다. mouth_open 은 빨간 입을 크게 벌리는 게 **의도한 변화**인데
    #    평균 색차는 그걸 "몸 색이 변질됐다"와 구분하지 못한다(실측 13.6 으로 기준 초과).
    #    그래서 둘로 나눈다:
    #      dcolor_med   중앙값 — 넓은 몸통이 지배한다. 이게 낮으면 몸 색은 그대로다
    #      changed_pct  ΔRGB>30 인 픽셀 비율 — 캐릭터의 몇 %가 바뀌었나.
    #                   입·눈만 바뀌면 한 자릿수, 재설계면 수십 %가 나온다
    dcolor = float(shared.mean())
    dcolor_med = float(np.median(shared))
    changed_pct = float((shared > 30).mean() * 100)

    # 차이가 세로 어디에 몰렸나. 얼굴(이 캐릭터는 중앙)에 집중돼야 정상이다.
    diff = d * both
    total = diff.sum()
    bands = {}
    if total > 0:
        third = CANVAS // 3
        for name, sl in (("top", slice(0, third)), ("mid", slice(third, 2 * third)), ("bot", slice(2 * third, CANVAS))):
            bands[name] = round(float(diff[sl].sum() / total * 100), 1)

    # 티끌은 **원본 해상도에서** 센다. _fit(512) 이후에 세면 82px 짜리가 축소 과정에서
    # 알파 임계값 아래로 뭉개져 0 개로 보인다(실제로 그렇게 놓칠 뻔했다).
    n_stray, px_stray = _strays(_mask(variant))
    return {
        "iou_raw": round(_iou(m_base_raw, m_var_raw), 1),
        "iou_aligned": round(_iou(m_base, m_var), 1),
        "dcolor": round(dcolor, 1),
        "dcolor_med": round(dcolor_med, 1),
        "changed_pct": round(changed_pct, 1),
        "diff_band": bands,
        "stray_blobs": n_stray,
        "stray_px": px_stray,
        "bbox_base": _bbox(_main_blob(m_base_raw)),
        "bbox_variant": _bbox(_main_blob(m_var_raw)),
    }


# ---------------------------------------------------------------------------
# 시각 산출물 — 최종 판정은 사람이 눈으로 한다 (LLM 청취 평가 실패의 교훈).
# ---------------------------------------------------------------------------
def _on_check(img: Image.Image, cell: int = 320) -> Image.Image:
    """투명 배경을 체커보드 위에 얹는다. 흰 배경에 올리면 누끼 실패가 안 보인다."""
    im = img.convert("RGBA")
    im.thumbnail((cell, cell), Image.LANCZOS)
    bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
    px = bg.load()
    for y in range(im.height):
        for x in range(im.width):
            if (x // 8 + y // 8) % 2:
                px[x, y] = (222, 222, 222, 255)
    bg.alpha_composite(im)
    return bg


def contact_sheet(sprites: dict, dst: Path, cell: int = 320) -> None:
    cells = [_on_check(img, cell) for img in sprites.values()]
    w = sum(c.width for c in cells)
    sheet = Image.new("RGBA", (w, max(c.height for c in cells)), (255, 255, 255, 255))
    x = 0
    for c in cells:
        sheet.paste(c, (x, 0))
        x += c.width
    sheet.save(dst)


def overlay_sheet(base: Image.Image, sprites: dict, dst: Path) -> None:
    """실루엣 겹침 — base 는 빨강, 변형은 파랑. 겹치면 보라, 어긋나면 색이 드러난다."""
    tiles = []
    mb = _mask(_align(base))
    for name, img in sprites.items():
        if name == BASE_NAME:
            continue
        mv = _mask(_align(img))
        rgb = np.full((CANVAS, CANVAS, 3), 255, np.uint8)
        rgb[..., 1] = np.where(mb | mv, 60, 255)  # 초록을 빼서 겹침을 보라로
        rgb[..., 0] = np.where(mv & ~mb, 90, 255 - (mb * 60))
        rgb[..., 2] = np.where(mb & ~mv, 90, 255 - (mv * 60))
        tiles.append(Image.fromarray(rgb))
    if not tiles:
        return
    sheet = Image.new("RGB", (CANVAS * len(tiles), CANVAS), (255, 255, 255))
    for i, t in enumerate(tiles):
        sheet.paste(t, (i * CANVAS, 0))
    sheet.save(dst)


# ---------------------------------------------------------------------------
# 생성
# ---------------------------------------------------------------------------
def _gen(src: Image.Image, prompt: str, label: str, attempts: int = 3) -> Image.Image:
    """빈 parts 응답이 6회 중 1회 온다(실측). 서버와 같은 이유로 재시도한다."""
    for i in range(1, attempts + 1):
        t0 = time.perf_counter()
        try:
            img = run_gemini(src, prompt, FLASH_MODEL)
            print(f"  [OK]   {label:<14} {time.perf_counter() - t0:5.1f}s  {img.size}")
            return img
        except Exception as e:
            print(f"  [FAIL] {label:<14} {time.perf_counter() - t0:5.1f}s  {type(e).__name__}: {e}")
            if i == attempts:
                raise
            time.sleep(2 * i)


def build(photo: Path) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"\n입력: {photo}")

    from PIL import ImageOps

    img = ImageOps.exif_transpose(Image.open(photo))
    img.thumbnail((1024, 1024), Image.LANCZOS)

    t0 = time.perf_counter()
    flat = flatten_on_white(remove_bg(img))
    print(f"  사전 누끼 {time.perf_counter() - t0:.1f}s")

    print("\nbase 생성 (서버와 같은 ACTIVE_PROMPT)")
    raw = _gen(flat, ACTIVE_PROMPT, BASE_NAME)
    # 누끼 전 원본을 남긴다. 누끼 파라미터를 고칠 때마다 325원을 다시 쓰지 않으려는 것.
    raw.save(OUT / f"raw_{BASE_NAME}.png")
    base = cutout(raw)
    base.save(OUT / f"{BASE_NAME}.png")

    # 변형 입력은 **누끼 전 RGB** 가 아니라 누끼 후를 흰 배경에 다시 얹은 것을 쓴다.
    # 알파를 그대로 넣으면 모델이 검은 배경으로 해석하는 경우가 있다(flatten_on_white 주석).
    base_flat = flatten_on_white(base)

    print("\n변형 생성 (base 를 입력으로)")
    sprites = {BASE_NAME: base}
    for name, prompt in SPRITE_PROMPTS.items():
        raw = _gen(base_flat, prompt, name)
        raw.save(OUT / f"raw_{name}.png")
        out = cutout(raw)
        out.save(OUT / f"{name}.png")
        sprites[name] = out
    return sprites


def recut() -> dict:
    """저장된 결과를 현재 cutout 설정으로 다시 자른다 (Gemini 재호출 없음).

    raw_*.png 가 있으면 그걸 쓰고, 없으면 기존 RGBA 를 흰 배경에 되붙여서 쓴다.
    되붙이기는 배경이 정확히 255 가 되므로 원본과 같은 결과가 나온다 — 다만 페더링된
    경계가 한 번 더 흰색과 섞이므로, 가능하면 raw 쪽을 남겨 두는 게 정확하다.
    """
    names = [BASE_NAME, *SPRITE_PROMPTS]
    sprites = {}
    for n in names:
        raw_p, cut_p = OUT / f"raw_{n}.png", OUT / f"{n}.png"
        if raw_p.is_file():
            src = Image.open(raw_p)
        elif cut_p.is_file():
            src = flatten_on_white(Image.open(cut_p))
        else:
            continue
        out = cutout(src)
        out.save(cut_p)
        sprites[n] = out
    print(f"  재누끼 {len(sprites)}장 완료")
    return sprites


def load_existing() -> dict:
    names = [BASE_NAME, *SPRITE_PROMPTS]
    found = {n: Image.open(OUT / f"{n}.png") for n in names if (OUT / f"{n}.png").is_file()}
    if BASE_NAME not in found:
        sys.exit(f"{OUT}/{BASE_NAME}.png 가 없습니다 — 먼저 생성하세요")
    return found


def report(sprites: dict) -> dict:
    base = sprites[BASE_NAME]
    rows = {n: compare(base, img) for n, img in sprites.items() if n != BASE_NAME}

    print("\n" + "=" * 74)
    print("R1 — 스프라이트 간 캐릭터 일관성")
    print("=" * 74)
    print(
        f"  {'스프라이트':<14}{'IoU(그대로)':>12}{'IoU(정렬후)':>12}"
        f"{'몸색차':>8}{'변경면적':>10}{'티끌':>8}"
    )
    print("  " + "-" * 74)
    for name, m in rows.items():
        stray = f"{m['stray_blobs']}개" if m["stray_blobs"] else "-"
        print(
            f"  {name:<14}{m['iou_raw']:>11.1f}%{m['iou_aligned']:>11.1f}%"
            f"{m['dcolor_med']:>8.1f}{m['changed_pct']:>9.1f}%{stray:>8}"
        )

    ious = [m["iou_aligned"] for m in rows.values()]
    meds = [m["dcolor_med"] for m in rows.values()]
    changed = [m["changed_pct"] for m in rows.values()]
    strays = sum(m["stray_blobs"] for m in rows.values())
    print("  " + "-" * 74)
    print(
        f"  최저 정렬 IoU {min(ious):.1f}%  /  최대 몸색차 {max(meds):.1f}  /  "
        f"최대 변경면적 {max(changed):.1f}%  /  티끌 {strays}개"
    )

    # 판정 기준: 정지 그림 비교가 아니라 **초당 20~30회 교체**가 전제다.
    # 실루엣이 5% 어긋나면 그 경계가 매 프레임 깜빡이고, 몸 색이 흔들리면 캐릭터가 떤다.
    # 변경면적은 입·눈만 바뀌었는지 보는 것 — 재설계면 수십 %가 나온다.
    verdict = (
        "PASS" if min(ious) >= 95 and max(meds) <= 8 and max(changed) <= 25 else "FAIL"
    )
    print(f"\n  판정: {verdict}  (기준: 정렬 IoU ≥95%, 몸색차 ≤8, 변경면적 ≤25%)")
    if strays:
        print(f"  ⚠️ 티끌 {strays}개 — AR 화면에 점으로 뜬다. cutout 에서 걸러야 한다.")
    if min(m["iou_raw"] for m in rows.values()) < 90 <= min(ious):
        print("  → 형태는 같고 위치·배율만 밀렸다. Android 에서 bbox 정렬로 복구 가능.")

    contact_sheet(sprites, OUT / "sheet.png")
    overlay_sheet(base, sprites, OUT / "overlay.png")
    print(f"\n  비교 이미지: {OUT / 'sheet.png'}")
    print(f"  실루엣 겹침: {OUT / 'overlay.png'}  (빨강=base 파랑=변형 보라=일치)")

    data = {"verdict": verdict, "sprites": rows}
    (OUT / "report.json").write_text(json.dumps(data, ensure_ascii=False, indent=2))
    return data


def selftest() -> None:
    """Gemini 를 부르지 않고 지표 코드만 점검한다.

    같은 이미지끼리 = 100%, 다른 프롬프트 결과끼리 = 그보다 낮게 나와야 한다.
    돈을 쓰기 전에 계산식이 뒤집혀 있지 않은지 확인하는 용도다.
    """
    old = ROOT / "out"
    a, b = old / "02_flash_v3.png", old / "02_flash.png"
    if not a.is_file():
        sys.exit(f"{a} 없음 — doll_stylize_test.py 산출물이 필요합니다")

    ia = cutout(Image.open(a))
    print("\n[자체 점검] 같은 이미지끼리")
    same = compare(ia, ia)
    print(f"  IoU {same['iou_raw']}% / {same['iou_aligned']}%  색차 {same['dcolor']}")
    assert same["iou_aligned"] == 100.0 and same["dcolor"] == 0.0, "동일 이미지가 100%가 아니다"

    if b.is_file():
        ib = cutout(Image.open(b))
        print("[자체 점검] v3 vs v1 (다른 프롬프트 = 달라야 정상)")
        d = compare(ia, ib)
        print(f"  IoU {d['iou_raw']}% / {d['iou_aligned']}%  색차 {d['dcolor']}  분포 {d['diff_band']}")
        assert d["iou_aligned"] < 100.0, "다른 이미지가 100% 로 나온다 — 계산이 틀렸다"

    print("\n  지표 코드 정상. 이제 실제 생성을 돌려도 된다.")


def main() -> None:
    load_env()
    args = sys.argv[1:]
    if args and args[0] == "--selftest":
        selftest()
        return
    if args and args[0] == "--measure":
        report(load_existing())
        return
    if args and args[0] == "--recut":
        report(recut())
        return
    if not args:
        print(__doc__)
        sys.exit(1)
    report(build(Path(args[0])))


if __name__ == "__main__":
    main()
