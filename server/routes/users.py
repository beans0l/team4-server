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
