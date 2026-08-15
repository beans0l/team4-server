"""파이프라인 실행 자원 — 스레드 풀 · 동시 실행 제한 · 워밍업 상태.

파이프라인은 CPU 를 오래 쓰는 블로킹 코드라 이벤트 루프에서 직접 돌리면
서버 전체가 멎는다. 여기서 스레드로 넘기고 동시 실행 수를 제한한다.
"""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

from PIL import Image

from server import config, pipeline
from server.errors import GeminiTimeoutError

log = logging.getLogger(__name__)

# 스레드 풀을 세마포어보다 하나 크게 잡는다.
# 같은 크기로 두면, 타임아웃난 작업 하나가 스레드를 계속 붙들고 있는 동안
# (파이썬은 실행 중인 스레드를 못 죽인다) 세마포어만 풀려서 다음 요청이 통과한 뒤
# executor 큐에서 굶는다. 그 요청도 타임아웃나고, 연쇄가 이어진다.
# 여유 스레드 하나가 그 고리를 끊어준다.
executor = ThreadPoolExecutor(
    max_workers=config.MAX_CONCURRENCY + 1, thread_name_prefix="pipeline"
)
_slots = asyncio.Semaphore(config.MAX_CONCURRENCY)

state = {"rembg": "cold"}


async def warmup() -> None:
    """기동 시 1회. 실패해도 서버는 뜬다 — 여기서 죽으면 무한 재시작에 빠진다."""
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(executor, pipeline.warmup)
        state["rembg"] = "loaded"
        log.info("배경 제거 모델 워밍업 완료: %s", config.REMBG_MODEL)
    except Exception as e:
        state["rembg"] = f"failed: {e}"
        log.exception("워밍업 실패 — 첫 요청이 느려집니다")


async def _acquire_and_run(fn, data: bytes):
    loop = asyncio.get_running_loop()
    async with _slots:
        return await loop.run_in_executor(executor, fn, data)


async def run_stylize_set(data: bytes) -> tuple[dict[str, Image.Image], dict, list[str]]:
    """스프라이트 5장. 예산과 동시 실행 제한은 1장 경로와 같은 것을 쓴다.

    ⚠️ 변형 4장은 pipeline 안쪽에서 자체 스레드 풀로 병렬 호출된다. 그 스레드는
       여기 세마포어를 다시 잡지 않는다 — 잡게 하면 동시 요청 2건이 서로의
       변형 슬롯을 기다리다 교착한다.
    """
    return await _run_with_budget(pipeline.stylize_set, data)


async def run_stylize(data: bytes) -> tuple[Image.Image, dict]:
    """파이프라인을 스레드에서 돌리고 예산을 초과하면 504 로 바꾼다.

    ⚠️ 예산은 **요청이 들어온 순간부터** 잰다. 슬롯을 기다리는 시간까지 포함해야
    한다 — 예전에는 `async with _slots` 안쪽만 재서, 앞에 밀린 요청이 많으면
    큐에서 60초 기다린 뒤 거기서 90초를 새로 받았다. 그러면 총 대기가 앞단
    프록시(nginx `proxy_read_timeout`, 또는 PaaS 의 요청 타임아웃)를 넘겨서,
    앱은 우리가 약속한 {code, message} JSON 이 아니라 프록시가 만든 HTML 504 를
    받게 된다. 그러므로 앞단 타임아웃은 PIPELINE_TIMEOUT_SEC 보다 커야 한다.
    """
    return await _run_with_budget(pipeline.stylize, data)


async def _run_with_budget(fn, data: bytes):
    try:
        return await asyncio.wait_for(
            _acquire_and_run(fn, data), timeout=config.PIPELINE_TIMEOUT_SEC
        )
    except asyncio.TimeoutError as e:
        # 파이썬은 실행 중인 스레드를 취소할 수 없다. 이미 시작된 작업은
        # 백그라운드에서 끝까지 돌다가 결과만 버려진다. 세마포어는 즉시 풀리지만
        # 스레드 자리는 그 작업이 끝나야 비므로, 여유 스레드 하나(위 +1)가
        # 다음 요청을 받아 연쇄 타임아웃을 막는다.
        raise GeminiTimeoutError("파이프라인 예산 초과") from e
