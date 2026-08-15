"""사진 -> 투명 PNG 스프라이트.

    ① 로드 + EXIF 회전 보정
    ② 리사이즈 (긴 변 1024)
    ③ 사전 누끼  rembg           1.2초    ← 일관성 장치. 생략 금지
    ④ 흰 배경 합성
    ⑤ Gemini 스타일 변환         12.4초   ← 재시도 대상
    ⑥ 코드 누끼  cutout          0.06초

③ 을 왜 빼면 안 되는가:
    배경 지시만으로도 손·배경은 사라진다. 배경 품질만 보면 ③ 없이도 성공한다.
    그런데 캐릭터가 매번 달라진다 — 같은 프롬프트로 3~4회 돌리면 자세·표정·무늬가
    전부 다르게 나온다(정면/누움/위에서 본 각도가 섞임). 실루엣을 미리 정리해서 주면
    모델이 형태를 해석할 여지가 사라져 일관성이 회복된다.
    나중에 립싱크용 입 3장이 초당 20~30회 교체될 때 이게 무너지면 캐릭터가 떨린다.
    **③ 은 배경 제거 수단이 아니라 일관성 장치다.**

진입점이 둘이다:
    stylize(data)      -> 1장. 롤백·회귀 테스트용 (SPRITE_SET=0)
    stylize_set(data)  -> 5장. 립싱크·표정용 (기본)
"""

import io
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
from PIL import Image, ImageOps

from ai.cutout import cutout
from ai.doll_stylize_test import flatten_on_white, remove_bg, run_gemini
from server import config
from server.errors import (
    GeminiEmptyError,
    InvalidImageError,
    MissingApiKeyError,
    QuotaExhaustedError,
    SpriteQualityError,
)
from server.prompts import BASE_SPRITE, SPRITE_PROMPT_SET, STYLIZE_PROMPT

log = logging.getLogger(__name__)


def _load(data: bytes) -> Image.Image:
    """① 디코딩 + EXIF 회전 보정.

    폰 카메라(CameraX)가 저장한 jpg 는 "90도 돌려서 보라"는 EXIF 방향 태그를 갖는다.
    실제로 테스트 사진의 orientation 값이 6 이었다. 보정하지 않으면 옆으로 누운
    인형을 그대로 그린다.

    ⚠️ 앱이 이미 보정했다면 여기서 또 하면 안 된다(두 번 돌아간다).
       연동 규약상 회전 보정은 이 서버 담당이고, 앱은 원본을 그대로 올린다.
    """
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as e:
        raise InvalidImageError(f"디코딩 실패: {e}") from e
    return ImageOps.exif_transpose(img)


def _resize(img: Image.Image, max_edge: int) -> Image.Image:
    """② 긴 변을 max_edge 로. 이미 작으면 건드리지 않는다(확대는 손해).

    ⚠️ 배경 제거를 빠르게 하려는 목적이 아니다. rembg 는 내부에서 고정 크기로
       리사이즈하므로 입력 해상도를 줄여도 추론은 빨라지지 않는다(실측: 512px 이
       1024px 보다 오히려 느렸다). 여기서 줄이는 이유는 전송·디코딩 비용과
       Gemini 업로드 크기 때문이다.
    """
    w, h = img.size
    longest = max(w, h)
    if longest <= max_edge:
        return img
    scale = max_edge / longest
    # max(1, ...) 이 없으면 극단적인 비율(예: 12000x8 파노라마)에서 짧은 변이 0 이 된다.
    # Pillow 는 높이 0 이미지를 그대로 만들고, 실패는 한참 뒤 rembg/numpy 안쪽에서
    # 엉뚱한 예외로 터진다 → 400 INVALID_IMAGE 대신 500 INTERNAL 이 나간다.
    return img.resize(
        (max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS
    )


def _segment(img: Image.Image) -> Image.Image:
    """③ 사전 누끼. 모델은 설정으로 바꿀 수 있다(기본 isnet-general-use)."""
    return remove_bg(img, model=config.REMBG_MODEL)


# 실측 정상값은 68.1~68.4%. 넉넉히 잡아도 이 밖이면 사람이 봐도 망가진 결과다.
MIN_TRANSPARENT_PCT = 15.0
MAX_TRANSPARENT_PCT = 92.0


def _check_sprite(img: Image.Image) -> float:
    """누끼 결과가 쓸 만한지 투명 비율로 판정하고, 아니면 예외를 던진다.

    cutout 은 어떤 입력에도 예외 없이 이미지를 돌려준다. 배경이 충분히 희지 않아
    가장자리 흰 덩어리를 하나도 못 찾으면 **전부 불투명한 이미지**가 나오는데,
    그게 200 으로 나가면 앱에서는 AR 위에 네모난 판이 뜨고 서버 로그에는 아무
    흔적도 없다. 조용한 실패를 시끄러운 실패로 바꾼다.
    """
    alpha = np.array(img.split()[-1])
    pct = float((alpha < 16).mean() * 100)
    if not (MIN_TRANSPARENT_PCT <= pct <= MAX_TRANSPARENT_PCT):
        raise SpriteQualityError(
            f"투명 비율 {pct:.1f}% (정상 {MIN_TRANSPARENT_PCT}~{MAX_TRANSPARENT_PCT}%) "
            f"— {'배경이 안 잘렸을' if pct < MIN_TRANSPARENT_PCT else '캐릭터까지 잘렸을'} 가능성"
        )
    return round(pct, 1)


def _classify(exc: Exception) -> str:
    """'quota' | 'rate_limit' | 'other'.

    429 를 전부 영구 소진으로 보면 안 된다. Gemini 는 **분당 요청 제한**에도 429 를
    주는데, 그건 잠깐 쉬면 풀린다. 예전에는 둘을 구분하지 않고 재시도를 통째로
    건너뛰어서, 1초만 기다리면 될 상황을 하드 에러로 올렸다.

    영구로 단정하는 건 무료 티어 서명(`limit: 0`)이 보일 때뿐이고, 나머지 429 는
    재시도한다. 재시도를 다 쓰고도 429 면 그때 QUOTA_EXHAUSTED 로 보고한다.

    ⚠️ 문자열에서 "429" 를 그냥 찾으면 안 된다. 요청 ID·바이트 수·타임스탬프에
       우연히 429 가 들어 있어도 걸린다. 단어 경계로 찾는다.
    """
    text = str(exc)
    upper = text.upper()
    is_429 = (
        getattr(exc, "code", None) == 429
        or "RESOURCE_EXHAUSTED" in upper
        or re.search(r"(?<!\d)429(?!\d)", text) is not None
    )
    if not is_429:
        return "other"
    if "FREE_TIER" in upper or "LIMIT: 0" in upper:
        return "quota"
    return "rate_limit"


def _stylize_with_retry(src: Image.Image, prompt: str, label: str = "base") -> Image.Image:
    """⑤ Gemini 호출 + 재시도.

    응답 parts 가 비어서 오는 일이 6회 중 1회 있다(실측). run_gemini 이 이때
    RuntimeError 를 던진다. 3회 재시도하면 실패율이 0.5% 아래로 내려간다.
    크레딧 소진(429)과 키 누락은 재시도해도 소용없으므로 즉시 올린다.

    프롬프트를 인자로 받는 이유: 같은 재시도·오류 분류 로직을 base 생성과 변형
    스프라이트 생성이 함께 쓴다. 예전에는 STYLIZE_PROMPT 를 함수 안에서 직접
    참조해서 재사용할 수 없었다.
    """
    if not config.GEMINI_API_KEY:
        raise MissingApiKeyError("GEMINI_API_KEY 미설정 (서버의 .env 확인)")

    last: Exception | None = None
    last_kind = "other"
    for attempt in range(1, config.GEMINI_ATTEMPTS + 1):
        try:
            return run_gemini(src, prompt, config.GEMINI_MODEL)
        except Exception as e:
            kind = _classify(e)
            if kind == "quota":
                raise QuotaExhaustedError(str(e)) from e
            last, last_kind = e, kind
            log.warning(
                "Gemini 실패 [%s] (%d/%d, %s): %s",
                label,
                attempt,
                config.GEMINI_ATTEMPTS,
                kind,
                e,
            )
            if attempt < config.GEMINI_ATTEMPTS:
                # 분당 제한은 더 오래 쉬어야 풀린다.
                base = 5 if kind == "rate_limit" else 1
                time.sleep(base * 2 ** (attempt - 1))

    # 끝까지 429 였다면 원인은 할당량이다. GEMINI_EMPTY 로 보고하면
    # "재시도해 보세요" 안내가 나가는데, 그건 도움이 안 된다.
    if last_kind == "rate_limit":
        raise QuotaExhaustedError(str(last)) from last
    raise GeminiEmptyError(str(last)) from last


def stylize(data: bytes) -> tuple[Image.Image, dict]:
    """사진 바이트 -> (투명 PNG 이미지, 단계별 소요시간 ms).

    블로킹 함수다. FastAPI 에서는 스레드로 넘겨서 호출한다.
    """
    timing: dict[str, float] = {}

    def step(name, fn, *args):
        t0 = time.perf_counter()
        result = fn(*args)
        timing[name] = round((time.perf_counter() - t0) * 1000)
        return result

    img = step("load", _load, data)
    img = step("resize", _resize, img, config.MAX_EDGE_PX)
    cut = step("rembg", _segment, img)
    flat = step("flatten", flatten_on_white, cut)
    drawn = step("gemini", _stylize_with_retry, flat, STYLIZE_PROMPT)
    final = step("cutout", cutout, drawn)

    timing["total"] = sum(timing.values())  # 합계는 단계 시간만 (아래 비율 추가 전에)
    timing["transparent_pct"] = _check_sprite(final)
    log.info("파이프라인 완료 %s", timing)
    return final, timing


def _variant(base_flat: Image.Image, name: str, prompt: str) -> Image.Image:
    """base 일러스트 -> 변형 스프라이트 1장. 스레드에서 병렬로 불린다."""
    drawn = _stylize_with_retry(base_flat, prompt, label=name)
    out = cutout(drawn)
    _check_sprite(out)
    return out


def stylize_set(data: bytes) -> tuple[dict[str, Image.Image], dict, list[str]]:
    """사진 -> {이름: 투명 PNG} 5장. ({스프라이트}, 소요시간, 실패한 이름).

    **원본에서 5장을 각각 뽑지 않는다.** base 를 먼저 만들고 그 결과 이미지를
    입력으로 변형시킨다(체이닝). 원본을 매번 새로 해석하게 하면 자세부터 달라지는데,
    이미 정리된 일러스트를 주면 해석의 여지가 없다. 이 방식으로 정렬 IoU 99.9% /
    몸 색차 4.9 를 확인했다(R1 해소, 2026-08-13).

        원본 -> rembg -> 흰배경 -> [Gemini] base ┬-> mouth_half ┐
                                                 ├-> mouth_open │ 병렬
                                                 ├-> happy      │
                                                 └-> sleepy     ┘

    부분 실패는 200 으로 나간다 — 변형 하나 때문에 등록 전체를 실패시키면 아이는
    28초를 기다린 뒤 아무것도 못 얻는다. 립싱크가 빠져도 인형은 나온다.
    다만 **조용히 빠지면 안 되므로** 실패한 이름을 돌려주고 로그에 남긴다.
    base 실패는 다르다 — 인형 자체가 없으므로 예외를 그대로 올린다.
    """
    timing: dict[str, float] = {}

    def step(name, fn, *args):
        t0 = time.perf_counter()
        result = fn(*args)
        timing[name] = round((time.perf_counter() - t0) * 1000)
        return result

    img = step("load", _load, data)
    img = step("resize", _resize, img, config.MAX_EDGE_PX)
    cut = step("rembg", _segment, img)
    flat = step("flatten", flatten_on_white, cut)
    drawn = step("gemini_base", _stylize_with_retry, flat, STYLIZE_PROMPT, "base")
    base = step("cutout_base", cutout, drawn)
    timing["transparent_pct"] = _check_sprite(base)

    sprites = {BASE_SPRITE: base}
    failed: list[str] = []

    # ⚠️ 변형 입력은 **투명 PNG 가 아니라 흰 배경에 다시 얹은 것**이어야 한다.
    #    알파를 그대로 넣으면 모델이 검은 배경으로 해석하는 경우가 있다.
    base_flat = step("flatten_base", flatten_on_white, base)

    t0 = time.perf_counter()
    # 파이프라인 스레드 풀과 별개다. 저쪽은 CPU/메모리가 병목이라 좁게 잡혀 있는데,
    # 여기는 순수 네트워크 대기라 같은 제한을 받을 이유가 없다.
    with ThreadPoolExecutor(
        max_workers=max(1, config.SPRITE_PARALLEL), thread_name_prefix="sprite"
    ) as pool:
        futures = {
            pool.submit(_variant, base_flat, name, prompt): name
            for name, prompt in SPRITE_PROMPT_SET.items()
        }
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                sprites[name] = fut.result()
            except QuotaExhaustedError:
                # 크레딧이 끊긴 것이라 나머지도 전부 실패한다. 실패 목록에 담아
                # 200 으로 내보내면 "일부만 안 됐다"로 보여서 원인을 못 찾는다.
                raise
            except Exception as e:
                failed.append(name)
                log.warning("스프라이트 '%s' 실패 — 이 장만 빼고 진행한다: %s", name, e)

    timing["gemini_variants"] = round((time.perf_counter() - t0) * 1000)
    timing["total"] = round(sum(v for k, v in timing.items() if k != "transparent_pct"))

    if failed:
        log.warning("스프라이트 %d장 실패: %s", len(failed), ", ".join(sorted(failed)))
    log.info("스프라이트 세트 완료 %d장 %s", len(sprites), timing)
    return sprites, timing, sorted(failed)


def warmup() -> None:
    """rembg 세션을 미리 로드한다.

    첫 요청에서 로딩하면 거기에만 수 초가 더 붙는다. 시연 직전 /healthz 로
    깨워두는 용도로도 쓴다.
    """
    _segment(Image.new("RGB", (32, 32), "white"))
