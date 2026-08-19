"""POST /doll/stylize — 인형 사진을 유아용 2D 캐릭터 투명 PNG 로.

기본은 립싱크·표정용 **5장**을 만든다. 입 3장이 초당 20~30회 교체돼야 인형이
말하는 것처럼 보이기 때문이다.

⚠️ 응답 형식은 앱과의 연동 계약이다. sprites / elapsed_ms 를 바꾸면 앱이 깨진다.
   sprites 는 계속 배열이고 **sprites[0] 은 항상 mouth_closed** 다 — 1장만 쓰던
   기존 클라이언트가 그대로 동작한다.
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, File, Request, UploadFile

from server import config, runtime, storage
from server.deps import get_current_user_id
from server.errors import InvalidImageError
from server.prompts import BASE_SPRITE

log = logging.getLogger(__name__)

router = APIRouter(tags=["doll"])

# 앱이 이 순서로 받는다. sprite_map 이 있으므로 순서에 의존할 필요는 없지만,
# 배열만 보는 기존 클라이언트를 위해 base 를 반드시 맨 앞에 둔다.
_ORDER = [BASE_SPRITE, "mouth_half", "mouth_open", "happy", "sleepy", "back"]


def _sorted_names(names) -> list[str]:
    """알려진 순서를 앞에, 모르는 이름(프롬프트 오버라이드로 추가된 것)은 뒤에."""
    known = [n for n in _ORDER if n in names]
    return known + sorted(n for n in names if n not in _ORDER)


@router.post("/doll/stylize")
async def stylize(
    request: Request,
    image: UploadFile = File(...),
    user_id: int = Depends(get_current_user_id),
):
    # user_id 는 지금은 인증 게이트 역할만 한다(비용이 드는 Gemini 호출을
    # 로그인한 사용자로 제한). 사용자별 호출 횟수 제한이 필요해지면 여기서
    # 카운트하면 된다.
    log.info("변환 요청 — user_id=%d", user_id)
    data = await image.read()
    if not data:
        raise InvalidImageError("빈 파일")
    if len(data) > config.MAX_UPLOAD_BYTES:
        raise InvalidImageError(f"{len(data)} 바이트 — 상한 초과")

    if config.SPRITE_SET:
        sprites, timing, failed = await runtime.run_stylize_set(data)
    else:
        # 롤백·개발용 1장 경로. 등록 1회가 325원에서 65원으로 떨어진다.
        img, timing = await runtime.run_stylize(data)
        sprites, failed = {BASE_SPRITE: img}, []

    # PNG 인코딩(수십 ms)과 디스크 쓰기는 블로킹이다. 여기서 그냥 부르면
    # 그동안 이벤트 루프가 멎어서 다른 요청도 /healthz 도 응답하지 못한다.
    # 파이프라인 스레드 풀과는 별도 스레드에서 돌린다 — 그 풀은 변환 작업 전용이라
    # 저장까지 밀어넣으면 동시 처리 수가 줄어든다.
    # 5장을 순차 저장하면 그만큼 이벤트 루프 밖에서 시간을 더 쓴다. 같이 보낸다.
    names = _sorted_names(sprites)
    keys = await asyncio.gather(
        *(
            asyncio.to_thread(storage.save_png, sprites[n], storage.new_key(n))
            for n in names
        )
    )

    # 앱에는 절대 URL 로 준다. base_url 의 http/https 는 X-Forwarded-Proto 로 결정되며,
    # uvicorn 이 그걸 신뢰하려면 --forwarded-allow-ips 가 필요하다(README 배포 ⑥).
    origin = str(request.base_url).rstrip("/")
    sprite_map = {n: origin + k for n, k in zip(names, keys)}

    if failed:
        log.warning("일부 스프라이트 없이 응답한다: %s", ", ".join(failed))

    return {
        # 배열은 기존 계약. [0] 이 base 다.
        "sprites": [sprite_map[n] for n in names],
        # 이름으로 찾는 쪽은 이걸 쓴다. 립싱크는 이름이 있어야 한다.
        "sprite_map": sprite_map,
        # 비어 있지 않으면 앱은 립싱크를 끄고 정지 이미지로 동작한다.
        "failed": failed,
        "elapsed_ms": timing["total"],
        "timing": timing,
    }
