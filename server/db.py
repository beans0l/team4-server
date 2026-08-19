
"""MySQL 연결.

pymysql 로 커넥션을 연다. 커넥션 풀은 쓰지 않는다 — MAX_CONCURRENCY(기본 2)로
동시 실행 수가 이미 낮아 풀링 이득이 크지 않다. 필요해지면 DBUtils.PooledDB 로
바꿀 것.
"""

import logging
from contextlib import contextmanager

import pymysql
from pymysql.cursors import DictCursor

from server import config

log = logging.getLogger(__name__)

def get_connection() -> pymysql.connections.Connection:
    return pymysql.connect(
        host=config.DB_HOST,
        port=config.DB_PORT,
        user=config.DB_USER,
        password=config.DB_PASSWORD,
        database=config.DB_NAME,
        cursorclass=DictCursor,
        autocommit=True,
    )


@contextmanager
def get_cursor():
    """with db.get_cursor() as cur: 로 쓴다. 커넥션은 블록이 끝나면 자동으로 닫힌다."""
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            yield cursor
    finally:
        conn.close()
