"""GOAL_TAGS CRUD — 오늘의 목표 등록/삭제.

수정은 없다 — 문구를 바꾸고 싶으면 삭제 후 다시 등록한다(요구사항).
등록은 같은 문구를 다시 쓰면 새 행을 만들지 않고 UNIQUE(user_id, content) 로
같은 행의 use_count 를 올린다 — 스키마에 use_count 가 있는 이유가 이거다.
"""

import asyncio
import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from server import db
from server.deps import get_current_user_id
from server.errors import NotFoundError

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/goal-tags", tags=["goal_tags"])


class GoalTagCreate(BaseModel):
    content: str


def _upsert_goal_tag(user_id: int, content: str) -> dict:
    with db.get_cursor() as cur:
        cur.execute(
            "INSERT INTO goal_tags (user_id, content, use_count) VALUES (%s, %s, 1) "
            "ON DUPLICATE KEY UPDATE use_count = use_count + 1",
            (user_id, content),
        )
        cur.execute(
            "SELECT id, content, use_count, created_at FROM goal_tags "
            "WHERE user_id = %s AND content = %s",
            (user_id, content),
        )
        return cur.fetchone()


def _list_goal_tags(user_id: int) -> list[dict]:
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT id, content, use_count, created_at FROM goal_tags "
            "WHERE user_id = %s ORDER BY use_count DESC, created_at DESC",
            (user_id,),
        )
        return cur.fetchall()


def _delete_goal_tag(user_id: int, tag_id: int) -> int:
    with db.get_cursor() as cur:
        cur.execute(
            "DELETE FROM goal_tags WHERE id = %s AND user_id = %s",
            (tag_id, user_id),
        )
        return cur.rowcount


@router.post("", status_code=201)
async def create_goal_tag(
    body: GoalTagCreate, user_id: int = Depends(get_current_user_id)
) -> dict:
    return await asyncio.to_thread(_upsert_goal_tag, user_id, body.content)


@router.get("")
async def list_goal_tags(user_id: int = Depends(get_current_user_id)) -> list[dict]:
    return await asyncio.to_thread(_list_goal_tags, user_id)


@router.delete("/{tag_id}", status_code=204)
async def delete_goal_tag(
    tag_id: int, user_id: int = Depends(get_current_user_id)
) -> None:
    deleted = await asyncio.to_thread(_delete_goal_tag, user_id, tag_id)
    if not deleted:
        raise NotFoundError()
