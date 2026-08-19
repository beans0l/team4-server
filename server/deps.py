"""로그인한 사용자를 요청에서 뽑아내는 FastAPI 의존성.

FRIENDS/GOAL_TAGS/MEALS 처럼 로그인한 사용자 소유 데이터를 다루는 라우트는
전부 `Depends(get_current_user_id)` 를 쓴다. `Authorization: Bearer <JWT>`
헤더를 검증하고 로그인 때 발급한 토큰의 sub(user_id) 를 돌려준다.
"""

import jwt
from fastapi import Header

from server import config
from server.errors import UnauthorizedError


def get_current_user_id(authorization: str | None = Header(default=None)) -> int:
    if not authorization or not authorization.startswith("Bearer "):
        raise UnauthorizedError("Authorization 헤더 없음")

    token = authorization.removeprefix("Bearer ").strip()
    try:
        payload = jwt.decode(token, config.JWT_SECRET, algorithms=[config.JWT_ALGORITHM])
    except jwt.PyJWTError as e:
        raise UnauthorizedError(f"토큰 검증 실패: {e}")

    return int(payload["sub"])
