"""순백 배경 결과물을 코드로 누끼 — rembg 대체 (44ms)

파이프라인:
    촬영 → [1] Gemini 스타일 변환 + 순백 배경 → [2] 이 모듈 → 투명 PNG

왜 rembg 를 안 쓰는가:
    Gemini 는 알파 채널을 만들지 못한다(투명 요구해도 RGB). 그러나 스타일 변환과 동시에
    순백 배경을 요구하면 모서리 RGB 254.7±0.6 으로 4/4 재현된다. 그 정도로 균일하면
    신경망 세그멘테이션이 필요 없다. rembg 12~25초 vs 이 방식 44ms, 결과는 사실상 동일.

사용법:
    ../.venv/bin/python cutout.py in.png out.png
    ../.venv/bin/python cutout.py --demo        # 기존 산출물로 rembg 와 비교
"""

import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.ndimage import binary_fill_holes, gaussian_filter, label

ROOT = Path(__file__).resolve().parent


def cutout(
    img: Image.Image,
    thresh: int = 238,
    feather: float = 0.6,
    min_blob_ratio: float = 0.005,
) -> Image.Image:
    """순백 배경을 투명으로. 캐릭터 내부의 흰색(눈 하이라이트)은 보존한다.

    단순 임계값(`>245` 를 전부 투명)을 쓰면 눈 하이라이트가 뚫린다.
    그래서 '가장자리에 연결된 흰색 덩어리'만 배경으로 판정한다.

    min_blob_ratio - 본체 대비 이 비율보다 작은 조각은 버린다.
        Gemini 출력의 맨 아랫줄에 RGB 234~242 짜리 압축 노이즈가 흩어져 나오는 일이
        있다(실측: 한 장에 96개, 중앙값 2px). 임계값 238 을 아슬아슬하게 못 넘겨서
        '배경 아님'으로 살아남는데, 그대로 두면 AR 화면에 캐릭터와 무관한 점들이 뜬다.
        임계값을 올려서 잡으면 안 된다 — 흰색·크림색 인형(R8)에서 몸통을 더 깎는다.
    """
    rgb = np.array(img.convert("RGB"))

    white = (rgb > thresh).all(axis=2)

    # 이미지 테두리에 닿아 있는 흰색 덩어리 = 진짜 배경
    lab, _ = label(white)
    edge_ids = set(lab[0, :]) | set(lab[-1, :]) | set(lab[:, 0]) | set(lab[:, -1])
    edge_ids.discard(0)
    bg = np.isin(lab, list(edge_ids))

    fg = binary_fill_holes(~bg)

    if min_blob_ratio:
        fg_lab, n = label(fg)
        if n > 1:
            sizes = np.bincount(fg_lab.ravel())
            sizes[0] = 0  # 배경 라벨. 이걸 0 으로 안 두면 전부 살아남는다
            fg = (sizes >= sizes.max() * min_blob_ratio)[fg_lab]

    alpha = (fg * 255).astype(np.float32)

    if feather:
        alpha = gaussian_filter(alpha, sigma=feather)  # 경계 계단현상 완화

    alpha = np.clip(alpha, 0, 255).astype(np.uint8)
    return Image.fromarray(np.dstack([rgb, alpha]), "RGBA")


def cutout_file(src: Path, dst: Path) -> dict:
    im = Image.open(src)
    t0 = time.perf_counter()
    res = cutout(im)
    ms = (time.perf_counter() - t0) * 1000
    res.save(dst)
    a = np.array(res.split()[-1])
    return {"ms": round(ms, 1), "transparent_pct": round((a < 16).mean() * 100, 1), "size": res.size}


def demo():
    out = ROOT / "out"
    targets = [
        (out / "02_flash_v3.png", "스타일 변환 결과 (RGB)"),
    ]
    print("=" * 66)
    print("코드 누끼 데모")
    print("=" * 66)
    for src, name in targets:
        if not src.exists():
            print(f"  {name}: {src.name} 없음 — doll_stylize_test.py 를 먼저 실행하세요")
            continue
        dst = out / f"cut_{src.stem}.png"
        r = cutout_file(src, dst)
        print(f"  {name}")
        print(f"    {r['ms']}ms / 투명 {r['transparent_pct']}% / {r['size']} -> {dst.name}")

    ref = out / "03_flash_v3_alpha.png"
    if ref.exists():
        a = np.array(Image.open(ref).convert("RGBA").split()[-1])
        print(f"\n  [참조] rembg 결과: 투명 {(a < 16).mean() * 100:.1f}% (12~25초 소요)")


def main():
    args = sys.argv[1:]
    if not args or args[0] == "--demo":
        demo()
        return
    if len(args) != 2:
        print(__doc__)
        sys.exit(1)
    r = cutout_file(Path(args[0]), Path(args[1]))
    print(f"{r['ms']}ms / 투명 {r['transparent_pct']}% / {r['size']} -> {args[1]}")


if __name__ == "__main__":
    main()
