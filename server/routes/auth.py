"""POST /api/auth/signup — 회원가입. POST /api/auth/login — 로그인(JWT 발급).

회원가입은 아이디(account) / 비밀번호 / 비밀번호 확인 / 이메일 / 이름 / 나이 / 키워드를
받는다. keyword 는 회원가입 폼에 미리 정해둔 10개 중 하나가 그대로 넘어온다 —
서버는 값을 고르지 않고 받은 그대로 저장한다. 비밀번호는 평문으로 저장하지 않고
bcrypt 로 해싱한 뒤 DB 에 넣는다. 성공해도 응답 본문은 없다(204).

로그인은 아이디 / 비밀번호를 받아 DB 의 해시와 대조하고, 맞으면 JWT 를 돌려준다.
서명 키(config.JWT_SECRET)는 .env 에서 랜덤한 값으로 채워야 한다 — 비어 있으면
누구나 빈 문자열로 서명해 토큰을 위조할 수 있다.

POST /api/auth/find-account — 아이디 찾기. 이메일을 받아 그 이메일로 가입된
account 를 그대로 돌려준다.

POST /api/auth/reset-password — 비밀번호 재설정. "원래 비밀번호를 보여주는" 기능은
만들지 않는다 — bcrypt 는 단방향 해시라 서버도 원문을 복원할 수 없고, 복원 가능하게
하려면 원문을 어딘가에 평문으로 남겨야 하는데 그러면 DB 유출 시 전체 계정이 털린다.
대신 account + 가입 시 등록한 email 이 일치하면 새 비밀번호로 바꿔준다. 이메일
인증코드 없이 email 값만 대조하는 약한 본인확인이다 — 이메일 발송 인프라(SMTP 등)가
생기면 코드 검증으로 올릴 것.

테이블 정의는 server/schema.sql 참조.
"""

import asyncio
import logging
import time

import bcrypt
import jwt
import pymysql
from fastapi import APIRouter
from pydantic import BaseModel

from server import config, db
from server.errors import (
    AccountEmailMismatchError,
    AccountNotFoundError,
    DuplicateUserError,
    InvalidCredentialsError,
    PasswordMismatchError,
)

log = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])

# 존재하지 않는 아이디로 로그인할 때 대조할 더미 해시(타이밍 공격 방지, 아래 참조).
# bcrypt 는 salt 마지막 글자가 임의 문자면 "Invalid salt" 로 거부하므로, 진짜
# bcrypt.hashpw 로 만든 유효한 해시여야 한다 — 문자열을 직접 짜맞추면 안 된다.
_DUMMY_HASH = bcrypt.hashpw(b"dummy-password-for-timing-safety", bcrypt.gensalt()).decode()


class SignupRequest(BaseModel):
    account: str
    password: str
    password_confirm: str
    email: str
    name: str
    age: int
    keyword: str


class LoginRequest(BaseModel):
    account: str
    password: str


class FindAccountRequest(BaseModel):
    email: str


class ResetPasswordRequest(BaseModel):
    account: str
    email: str
    new_password: str
    new_password_confirm: str


def _insert_user(
    account: str,
    password_hash: str,
    email: str,
    name: str,
    age: int,
    keyword: str,
) -> None:
    with db.get_cursor() as cur:
        cur.execute(
            "INSERT INTO users (account, password_hash, email, name, age, keyword) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (account, password_hash, email, name, age, keyword),
        )


def _fetch_user(account: str) -> dict | None:
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT id, password_hash FROM users WHERE account = %s",
            (account,),
        )
        return cur.fetchone()


def _fetch_account_by_email(email: str) -> str | None:
    with db.get_cursor() as cur:
        cur.execute("SELECT account FROM users WHERE email = %s", (email,))
        row = cur.fetchone()
        return row["account"] if row else None


def _account_email_matches(account: str, email: str) -> bool:
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT id FROM users WHERE account = %s AND email = %s",
            (account, email),
        )
        return cur.fetchone() is not None


def _update_password(account: str, password_hash: str) -> None:
    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE users SET password_hash = %s WHERE account = %s",
            (password_hash, account),
        )


def _make_token(user_id: int, account: str) -> str:
    now = int(time.time())
    payload = {
        "sub": str(user_id),
        "account": account,
        "iat": now,
        "exp": now + config.JWT_EXPIRE_MINUTES * 60,
    }
    return jwt.encode(payload, config.JWT_SECRET, algorithm=config.JWT_ALGORITHM)


@router.post("/api/auth/signup", status_code=204)
async def signup(body: SignupRequest) -> None:
    if body.password != body.password_confirm:
        raise PasswordMismatchError()

    # bcrypt 해싱은 의도적으로 무거운(느린) 연산이라 이벤트 루프를 막는다.
    # DB 접속(pymysql, 동기)도 마찬가지라 같이 스레드로 뺀다.
    password_hash = await asyncio.to_thread(
        lambda: bcrypt.hashpw(body.password.encode(), bcrypt.gensalt()).decode()
    )

    try:
        await asyncio.to_thread(
            _insert_user,
            body.account,
            password_hash,
            body.email,
            body.name,
            body.age,
            body.keyword,
        )
    except pymysql.err.IntegrityError:
        raise DuplicateUserError()


@router.post("/api/auth/login")
async def login(body: LoginRequest) -> dict:
    user = await asyncio.to_thread(_fetch_user, body.account)

    # 아이디가 없어도 bcrypt.checkpw 를 더미 해시로 한 번 돌린다. 그냥 바로
    # 401 을 던지면 "아이디 없음"이 "비밀번호 틀림"보다 훨씬 빨리 응답해서,
    # 응답 시간 차이로 존재하는 아이디를 추측당할 수 있다(타이밍 공격).
    stored_hash = user["password_hash"] if user else _DUMMY_HASH
    ok = await asyncio.to_thread(
        bcrypt.checkpw, body.password.encode(), stored_hash.encode()
    )
    if not user or not ok:
        raise InvalidCredentialsError()

    token = await asyncio.to_thread(_make_token, user["id"], body.account)
    return {"token": token}


@router.post("/api/auth/find-account")
async def find_account(body: FindAccountRequest) -> dict:
    account = await asyncio.to_thread(_fetch_account_by_email, body.email)
    if account is None:
        raise AccountNotFoundError()
    return {"account": account}


@router.post("/api/auth/reset-password", status_code=204)
async def reset_password(body: ResetPasswordRequest) -> None:
    if body.new_password != body.new_password_confirm:
        raise PasswordMismatchError()

    matches = await asyncio.to_thread(
        _account_email_matches, body.account, body.email
    )
    if not matches:
        raise AccountEmailMismatchError()

    password_hash = await asyncio.to_thread(
        lambda: bcrypt.hashpw(body.new_password.encode(), bcrypt.gensalt()).decode()
    )
    await asyncio.to_thread(_update_password, body.account, password_hash)
