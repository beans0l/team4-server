"""MEALS — 미션 로드맵에 쌓이는 '한 끼' 기록.

'미션'은 고정된 카탈로그가 아니라 사용자가 몇 번째로 먹은 식사인지다.
mission_no 는 그 사용자의 기존 meals 개수 + 1 로 **서버가 정한다** — 클라이언트가
직접 번호를 매기게 하면 중복·건너뜀·기기 간 레이스가 생겨 로드맵 번호가 꼬인다.

흐름:
  1. 목표를 고르고 식사를 시작하면 POST /api/meals 를 부른다. 이 순간 서버가
     started_at 을 찍고, goals 는 아직 달성 여부 없이 문구만 들어간다.
  2. 식사가 끝나고 달성 여부를 체크하면 PATCH /api/meals/{id}/end 를 부른다.
     이 순간 서버가 ended_at 을 찍고, goals 를 달성 여부가 포함된 형태로
     덮어쓴다. 그 전까지는 ended_at 이 NULL — 미완료 식사다.

friend_id 는 선택이다(누구와 먹었는지). 넘겼다면 로그인한 사용자 소유의
FRIENDS 인지 확인한다 — 안 그러면 남의 friend_id 를 끼워 넣어 관계를
위조할 수 있다.
"""

import asyncio
import json
import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from server import db
from server.deps import get_current_user_id
from server.errors import NotFoundError, PipelineError

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/meals", tags=["meals"])


class MealAlreadyEndedError(PipelineError):
    code = "MEAL_ALREADY_ENDED"
    status = 409
    message = "이미 끝난 식사예요."


class MealCreate(BaseModel):
    friend_id: int | None = None
    goals: list[str]


class GoalResult(BaseModel):
    content: str
    achieved: bool


class MealEnd(BaseModel):
    goals: list[GoalResult]


def _friend_belongs_to_user(user_id: int, friend_id: int) -> bool:
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT id FROM friends WHERE id = %s AND user_id = %s",
            (friend_id, user_id),
        )
        return cur.fetchone() is not None


def _row_to_meal(row: dict) -> dict:
    row["goals"] = json.loads(row["goals"])
    return row


def _insert_meal(user_id: int, friend_id: int | None, goals: list[str]) -> dict:
    with db.get_cursor() as cur:
        # 같은 사용자가 앱을 두 대에서 동시에 열고 거의 동시에 식사를 시작하면
        # 둘 다 같은 mission_no 를 받을 수 있다(SELECT-then-INSERT 레이스).
        # 이 앱은 한 아이가 한 기기로 쓰는 걸 전제해서 지금은 감수한다.
        cur.execute(
            "SELECT COALESCE(MAX(mission_no), 0) + 1 AS next_no "
            "FROM meals WHERE user_id = %s",
            (user_id,),
        )
        mission_no = cur.fetchone()["next_no"]

        cur.execute(
            "INSERT INTO meals (user_id, friend_id, mission_no, goals) "
            "VALUES (%s, %s, %s, %s)",
            (user_id, friend_id, mission_no, json.dumps(goals)),
        )
        meal_id = cur.lastrowid
        cur.execute(
            "SELECT id, friend_id, mission_no, goals, started_at, ended_at "
            "FROM meals WHERE id = %s",
            (meal_id,),
        )
        return _row_to_meal(cur.fetchone())


def _list_meals(user_id: int) -> list[dict]:
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT id, friend_id, mission_no, goals, started_at, ended_at "
            "FROM meals WHERE user_id = %s ORDER BY mission_no ASC",
            (user_id,),
        )
        return [_row_to_meal(row) for row in cur.fetchall()]


def _fetch_meal(user_id: int, meal_id: int) -> dict | None:
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT id, friend_id, mission_no, goals, started_at, ended_at "
            "FROM meals WHERE id = %s AND user_id = %s",
            (meal_id, user_id),
        )
        row = cur.fetchone()
        return _row_to_meal(row) if row else None


def _end_meal(user_id: int, meal_id: int, goals: list[dict]) -> str:
    """반환값: "ok" / "not_found" / "already_ended".

    db.get_cursor() 는 autocommit 이라 SELECT ... FOR UPDATE 로 락을 걸어도
    바로 풀려버려 두 문장 사이에 레이스가 남는다. 대신 UPDATE 문 자체에
    ended_at IS NULL 조건을 걸어 "아직 안 끝난 것만" 원자적으로 갱신한다.
    """
    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE meals SET goals = %s, ended_at = CURRENT_TIMESTAMP "
            "WHERE id = %s AND user_id = %s AND ended_at IS NULL",
            (json.dumps(goals), meal_id, user_id),
        )
        if cur.rowcount == 1:
            return "ok"

        cur.execute(
            "SELECT ended_at FROM meals WHERE id = %s AND user_id = %s",
            (meal_id, user_id),
        )
        row = cur.fetchone()
        return "not_found" if row is None else "already_ended"


@router.post("", status_code=201)
async def create_meal(
    body: MealCreate, user_id: int = Depends(get_current_user_id)
) -> dict:
    if body.friend_id is not None:
        owns = await asyncio.to_thread(_friend_belongs_to_user, user_id, body.friend_id)
        if not owns:
            raise NotFoundError("friend_id 가 없거나 내 것이 아님")
    return await asyncio.to_thread(_insert_meal, user_id, body.friend_id, body.goals)


@router.get("")
async def list_meals(user_id: int = Depends(get_current_user_id)) -> list[dict]:
    return await asyncio.to_thread(_list_meals, user_id)


@router.get("/{meal_id}")
async def get_meal(meal_id: int, user_id: int = Depends(get_current_user_id)) -> dict:
    meal = await asyncio.to_thread(_fetch_meal, user_id, meal_id)
    if meal is None:
        raise NotFoundError()
    return meal


@router.patch("/{meal_id}/end")
async def end_meal(
    meal_id: int, body: MealEnd, user_id: int = Depends(get_current_user_id)
) -> dict:
    goals = [g.model_dump() for g in body.goals]
    result = await asyncio.to_thread(_end_meal, user_id, meal_id, goals)
    if result == "not_found":
        raise NotFoundError()
    if result == "already_ended":
        raise MealAlreadyEndedError()
    return await asyncio.to_thread(_fetch_meal, user_id, meal_id)
