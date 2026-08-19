"""FastAPI 앱 조립.

    엔드포인트를 추가할 때는 server/routes/ 에 모듈을 만들고 여기서
    include_router() 만 하면 된다. CRUD·DB 도 같은 방식으로 붙인다.
"""

import logging

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from server import config, runtime
from server.errors import PipelineError
from server.prompts import (
    DOLL_PERSONA_SOURCE,
    SPRITE_PROMPT_SET,
    SPRITE_PROMPT_SOURCE,
    STYLIZE_PROMPT_SOURCE,
)
from server.routes import auth, friends, goal_tags, meals, stylize, talk, users

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s"
)
log = logging.getLogger("server")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(
        "기동 — rembg=%s / gemini=%s / prompt=%s / sprites=%d(%s) "
        "/ live=%s voice=%s persona=%s",
        config.REMBG_MODEL,
        config.GEMINI_MODEL,
        STYLIZE_PROMPT_SOURCE,
        (1 + len(SPRITE_PROMPT_SET)) if config.SPRITE_SET else 1,
        SPRITE_PROMPT_SOURCE,
        config.LIVE_MODEL,
        config.DOLL_VOICE,
        DOLL_PERSONA_SOURCE,
    )
    if not config.SPRITE_SET:
        log.warning(
            "SPRITE_SET=0 — 스프라이트를 1장만 만듭니다. 비용은 65원으로 줄지만 "
            "립싱크용 입 모양이 없어 인형 입이 움직이지 않습니다."
        )
    # 기동할 때 알려주지 않으면, 첫 요청이 실패하고 나서야 알게 된다.
    if not config.GEMINI_API_KEY:
        log.error(
            "GEMINI_API_KEY 가 없습니다. 모든 변환 요청이 503 으로, 대화 연결은 "
            "LIVE_UNAVAILABLE 로 실패합니다. 서버의 .env 파일을 확인하세요."
        )
    if not config.JWT_SECRET:
        log.error(
            "JWT_SECRET 이 없습니다. 빈 문자열로 토큰을 서명하게 되어 누구나 "
            "위조할 수 있습니다. 서버의 .env 파일에 랜덤한 값을 넣으세요."
        )
    await runtime.warmup()
    yield


app = FastAPI(title="Doll AI Server", lifespan=lifespan)

app.include_router(auth.router)
app.include_router(friends.router)
app.include_router(goal_tags.router)
app.include_router(meals.router)
app.include_router(stylize.router)
app.include_router(talk.router)
app.include_router(users.router)

# 결과 PNG 서빙. 앱이 sprites[0] URL 로 여기에 직접 접근한다.
# 마운트 전에 디렉토리를 만들어야 한다 — StaticFiles 는 없는 경로에 대해
# 기동 시점에 예외를 던진다.
Path(config.LOCAL_STORAGE_DIR).mkdir(parents=True, exist_ok=True)
app.mount("/files", StaticFiles(directory=config.LOCAL_STORAGE_DIR), name="files")


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"code": code, "message": message})


# 본문 크기 제한은 **라우트에 들어오기 전에** 걸어야 한다.
# 핸들러 안에서 len(data) 를 재는 건 늦다 — FastAPI 가 UploadFile 을 만들면서
# 이미 본문 전체를 읽어 SpooledTemporaryFile 로 넘긴 뒤이기 때문이다. 요청 1건이
# 이미 약 1GB 를 쓰는 서버라, 거대한 업로드 하나가 프로세스를 OOM 으로 죽이면서
# 같이 돌던 변환 작업까지 끌고 내려간다.
#
# ⚠️ Content-Length 가 없는 청크 전송은 이 방식으로 못 막는다. 완전한 방어는
#    아니고 첫 번째 방어선이다.
_MAX_BODY = config.MAX_UPLOAD_BYTES + 1024 * 1024  # multipart 부가 정보 여유


@app.middleware("http")
async def _limit_body_size(request: Request, call_next):
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > _MAX_BODY:
        log.warning("본문 %s 바이트 — 상한 초과로 거절", declared)
        return _error(400, "INVALID_IMAGE", "사진 용량이 너무 커요.")
    return await call_next(request)


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError):
    """FastAPI 기본 422 는 {"detail": [...]} 라서 code 가 없다.

    연동 규약상 호출하는 쪽은 code 만 보고 분기하므로, 필드명을 잘못 보냈을 때
    파싱할 수 없는 응답이 나가면 안 된다. 400 으로 맞춰 준다.

    /doll/stylize 는 image 필드 하나만 받으므로 그 실패는 기존처럼 INVALID_IMAGE
    로 응답한다. 그 외 라우트(auth/friends/goal-tags/meals/users)의 검증 실패까지
    "사진이 첨부되지 않았어요"로 답하면 회원가입 필드 누락 같은 것도 이미지
    문제로 잘못 안내하게 되므로 일반 VALIDATION_ERROR 로 분리한다.
    """
    errors = exc.errors()
    log.warning("요청 형식 오류: %s", errors)
    if any(err["loc"] and err["loc"][-1] == "image" for err in errors):
        return _error(400, "INVALID_IMAGE", "사진이 첨부되지 않았어요. (필드명: image)")
    fields = ", ".join(
        ".".join(str(p) for p in err["loc"] if p != "body") for err in errors
    )
    return _error(400, "VALIDATION_ERROR", f"입력값을 확인해 주세요. ({fields})")


@app.exception_handler(PipelineError)
async def _pipeline_error(request: Request, exc: PipelineError):
    log.warning("%s: %s", exc.code, exc.detail)
    return _error(exc.status, exc.code, exc.message)


@app.exception_handler(Exception)
async def _unexpected(request: Request, exc: Exception):
    log.exception("처리되지 않은 오류")
    return _error(500, "INTERNAL", "알 수 없는 오류가 발생했어요.")


@app.get("/")
async def root():
    """제출한 서버 주소를 브라우저로 열었을 때 보이는 안내.

    이게 없으면 루트가 404 {"detail":"Not Found"} 라서, 서버가 멀쩡히 떠 있는데도
    주소를 잘못 받았거나 죽은 것처럼 보인다. API 서버라 동작상 문제는 아니지만
    심사·시연에서 이 주소를 그대로 여는 사람이 있다.
    """
    return {
        "service": "인형 AI 서버",
        "status": "running",
        "docs": "/docs",
        "health": "/healthz",
    }


@app.get("/healthz")
async def healthz():
    return {
        "status": "ok",
        # 값은 노출하지 않는다. 설정 여부만.
        "api_key": bool(config.GEMINI_API_KEY),
        "rembg": runtime.state["rembg"],
        "rembg_model": config.REMBG_MODEL,
        "gemini_model": config.GEMINI_MODEL,
        "prompt_source": STYLIZE_PROMPT_SOURCE,
        # 스프라이트 세트. sprite_count 가 1 이면 SPRITE_SET=0 으로 떠 있는 것이고,
        # 그 상태로 시연하면 인형 입이 움직이지 않는다.
        "sprite_count": (1 + len(SPRITE_PROMPT_SET)) if config.SPRITE_SET else 1,
        "sprite_prompt_source": SPRITE_PROMPT_SOURCE,
        # 대화(WS /doll/talk). 어떤 페르소나로 떠 있는지가 안 보이면, 파일을
        # 고쳤는데 반영이 안 된 상태로 시연에 들어가게 된다.
        "live_model": config.LIVE_MODEL,
        "doll_voice": config.DOLL_VOICE,
        "persona_source": DOLL_PERSONA_SOURCE,
        "max_talk_sessions": config.MAX_TALK_SESSIONS,
    }
