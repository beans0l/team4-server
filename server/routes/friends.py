"""FRIENDS CRUD — 등록/삭제/이름 수정.

등록 흐름: 클라이언트가 먼저 POST /doll/stylize 로 사진을 보내 AI 누끼 이미지
URL 을 받고, 그 URL 과 이름을 여기(POST /api/friends)로 보내면 등록이 끝난다.
여기서 AI 파이프라인을 다시 돌리지 않는다 — /doll/stylize 가 이미 한 일이다.

로그인한 사용자 소유 데이터라 전부 Depends(get_current_user_id) 로 user_id 를 받고,
쿼리에 user_id 조건을 같이 건다 — 그래야 남의 친구를 조회/수정/삭제할 수 없다.
"""

import asyncio
import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from server import db
from server.deps import get_current_user_id
from server.errors import NotFoundError

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/friends", tags=["friends"])


class FriendCreate(BaseModel):
    name: str
    image_url: str


class FriendUpdate(BaseModel):
    name: str


def _insert_friend(user_id: int, name: str, image_url: str) -> dict:
    with db.get_cursor() as cur:
        cur.execute(
            "INSERT INTO friends (user_id, name, image_url) VALUES (%s, %s, %s)",
            (user_id, name, image_url),
        )
        friend_id = cur.lastrowid
        cur.execute(
            "SELECT id, name, image_url, created_at FROM friends WHERE id = %s",
            (friend_id,),
        )
        return cur.fetchone()


def _list_friends(user_id: int) -> list[dict]:
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT id, name, image_url, created_at FROM friends "
            "WHERE user_id = %s ORDER BY created_at DESC",
            (user_id,),
        )
        return cur.fetchall()


def _update_friend_name(user_id: int, friend_id: int, name: str) -> dict | None:
    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE friends SET name = %s WHERE id = %s AND user_id = %s",
            (name, friend_id, user_id),
        )
        if cur.rowcount == 0:
            return None
        cur.execute(
            "SELECT id, name, image_url, created_at FROM friends WHERE id = %s",
            (friend_id,),
        )
        return cur.fetchone()


def _delete_friend(user_id: int, friend_id: int) -> int:
    with db.get_cursor() as cur:
        cur.execute(
            "DELETE FROM friends WHERE id = %s AND user_id = %s",
            (friend_id, user_id),
        )
        return cur.rowcount


@router.post("", status_code=201)
async def create_friend(
    body: FriendCreate, user_id: int = Depends(get_current_user_id)
) -> dict:
    return await asyncio.to_thread(_insert_friend, user_id, body.name, body.image_url)


@router.get("")
async def list_friends(user_id: int = Depends(get_current_user_id)) -> list[dict]:
    return await asyncio.to_thread(_list_friends, user_id)


@router.patch("/{friend_id}")
async def update_friend(
    friend_id: int, body: FriendUpdate, user_id: int = Depends(get_current_user_id)
) -> dict:
    friend = await asyncio.to_thread(_update_friend_name, user_id, friend_id, body.name)
    if friend is None:
        raise NotFoundError()
    return friend


@router.delete("/{friend_id}", status_code=204)
async def delete_friend(
    friend_id: int, user_id: int = Depends(get_current_user_id)
) -> None:
    deleted = await asyncio.to_thread(_delete_friend, user_id, friend_id)
    if not deleted:
        raise NotFoundError()
