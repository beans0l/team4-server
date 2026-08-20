"""GET /api/users/me — 내 정보 조회. PATCH /api/users/me — 개인정보 수정.

계정 아이디(account) / 비밀번호 / 이름 / 나이 / keyword 를 전부 바꿀 수 있다.
전부 선택 필드라 보낸 것만 바뀐다(부분 수정).

account 나 password 를 바꿀 때는 current_password 가 반드시 있어야 한다.
JWT 만 탈취해도 계정을 빼앗을 수 있는 걸 막기 위해서다 — 로그인 세션이 있다고
해서 계정 자체의 주인이라는 뜻은 아니다.
"""

import asyncio
import logging

import bcrypt
import pymysql
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from server import db
from server.deps import get_current_user_id
from server.errors import (
    CurrentPasswordRequiredError,
    DuplicateUserError,
    InvalidCredentialsError,
    PasswordMismatchError,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/users", tags=["users"])


class NameUpdate(BaseModel):
    """PATCH /me/name 전용. 앱이 보내는 모양 그대로다."""

    name: str


class KeywordUpdate(BaseModel):
    """PATCH /me/keyword 전용.

    🔴 앱은 **배열**로 보내는데(`{"keyword": ["공룡","딸기"]}`) 컬럼은 문자열이다.
       어느 한쪽이 변환해야 하는데 서버가 한다 — 앱을 고치면 재설치가 필요하고,
       변환 규칙(쉼표 구분)은 어차피 서버가 정하는 것이다.
    문자열로 보내는 클라이언트(curl·스크립트)도 받아준다.
    """

    keyword: list[str] | str


class ProfileUpdate(BaseModel):
    current_password: str | None = None
    account: str | None = None
    password: str | None = None
    password_confirm: str | None = None
    name: str | None = None
    age: int | None = None
    keyword: str | None = None


def _fetch_profile(user_id: int) -> dict | None:
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT id, account, email, name, age, keyword, created_at "
            "FROM users WHERE id = %s",
            (user_id,),
        )
        return cur.fetchone()


def _fetch_password_hash(user_id: int) -> str | None:
    with db.get_cursor() as cur:
        cur.execute("SELECT password_hash FROM users WHERE id = %s", (user_id,))
        row = cur.fetchone()
        return row["password_hash"] if row else None


def _update_user(user_id: int, fields: dict) -> None:
    set_clause = ", ".join(f"{col} = %s" for col in fields)
    with db.get_cursor() as cur:
        cur.execute(
            f"UPDATE users SET {set_clause} WHERE id = %s",
            (*fields.values(), user_id),
        )


@router.get("/me")
async def get_profile(user_id: int = Depends(get_current_user_id)) -> dict:
    return await asyncio.to_thread(_fetch_profile, user_id)


@router.patch("/me/name")
async def update_name(
    body: NameUpdate, user_id: int = Depends(get_current_user_id)
) -> dict:
    """아이 이름만 바꾼다(설정 → 이름 변경).

    PATCH /me 로도 되지만 앱이 이 경로를 부르고 있어서 맞춰 둔다. 자격증명
    (account/password)을 건드리지 않으므로 current_password 를 요구하지 않는다.
    """
    await asyncio.to_thread(_update_user, user_id, {"name": body.name})
    return await asyncio.to_thread(_fetch_profile, user_id)


@router.patch("/me/keyword")
async def update_keyword(
    body: KeywordUpdate, user_id: int = Depends(get_current_user_id)
) -> dict:
    """관심사만 바꾼다(설정 → 관심사 변경).

    ⚠️ 컬럼이 좁으면(초기 스키마는 VARCHAR(30)) 관심사 3~4개에서 **조용히 잘린다.**
       MySQL 이 기본 설정에서 경고만 내고 자르기 때문에 앱에는 200 이 나가고
       사용자는 왜 관심사가 사라졌는지 알 수 없다. 컬럼을 넓혀 둘 것.
    """
    raw = body.keyword
    keyword = ",".join(k.strip() for k in raw if k.strip()) if isinstance(raw, list) else raw
    await asyncio.to_thread(_update_user, user_id, {"keyword": keyword})
    return await asyncio.to_thread(_fetch_profile, user_id)


@router.patch("/me")
async def update_profile(
    body: ProfileUpdate, user_id: int = Depends(get_current_user_id)
) -> dict:
    changing_credentials = body.account is not None or body.password is not None
    if changing_credentials:
        if not body.current_password:
            raise CurrentPasswordRequiredError()
        stored_hash = await asyncio.to_thread(_fetch_password_hash, user_id)
        ok = await asyncio.to_thread(
            bcrypt.checkpw, body.current_password.encode(), stored_hash.encode()
        )
        if not ok:
            raise InvalidCredentialsError()

    if body.password is not None and body.password != body.password_confirm:
        raise PasswordMismatchError()

    fields: dict = {}
    if body.account is not None:
        fields["account"] = body.account
    if body.password is not None:
        fields["password_hash"] = await asyncio.to_thread(
            lambda: bcrypt.hashpw(body.password.encode(), bcrypt.gensalt()).decode()
        )
    if body.name is not None:
        fields["name"] = body.name
    if body.age is not None:
        fields["age"] = body.age
    if body.keyword is not None:
        fields["keyword"] = body.keyword

    if fields:
        try:
            await asyncio.to_thread(_update_user, user_id, fields)
        except pymysql.err.IntegrityError:
            raise DuplicateUserError()

    return await asyncio.to_thread(_fetch_profile, user_id)
