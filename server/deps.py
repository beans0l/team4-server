"""로그인한 사용자를 요청에서 뽑아내는 의존성.

FRIENDS/GOAL_TAGS/MEALS/STYLIZE 처럼 로그인한 사용자 소유 데이터를 다루는
HTTP 라우트는 `Depends(get_current_user_id)` 를 쓴다. `Authorization: Bearer <JWT>`
헤더를 검증하고 로그인 때 발급한 토큰의 sub(user_id) 를 돌려준다.

WS(/doll/talk)는 브라우저/클라이언트가 핸드셰이크에 커스텀 헤더를 못 싣는
경우가 많아 쿼리파라미터(`?token=`)로 받는다 — `get_user_id_from_token` 을 쓴다.
둘 다 같은 검증 로직(`_decode`)을 공유한다.
"""

import jwt
from fastapi import Header

from server import config
from server.errors import UnauthorizedError


def _decode(token: str) -> int:
    try:
        payload = jwt.decode(token, config.JWT_SECRET, algorithms=[config.JWT_ALGORITHM])
    except jwt.PyJWTError as e:
        raise UnauthorizedError(f"토큰 검증 실패: {e}")
    return int(payload["sub"])


def get_current_user_id(authorization: str | None = Header(default=None)) -> int:
    if not authorization or not authorization.startswith("Bearer "):
        raise UnauthorizedError("Authorization 헤더 없음")
    return _decode(authorization.removeprefix("Bearer ").strip())


def get_user_id_from_token(token: str | None) -> int:
    if not token:
        raise UnauthorizedError("token 파라미터 없음")
    return _decode(token)
