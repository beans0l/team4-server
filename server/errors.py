"""파이프라인 예외 → HTTP 응답 매핑.

앱이 `code` 문자열만 보고 분기할 수 있게 고정한다.
앱과의 연동 계약이므로 값을 함부로 바꾸지 말 것.
"""


class PipelineError(Exception):
    """서버가 클라이언트에게 설명할 수 있는 실패."""

    code = "INTERNAL"
    status = 500
    message = "알 수 없는 오류가 발생했어요."

    def __init__(self, detail: str | None = None):
        super().__init__(detail or self.message)
        self.detail = detail


class InvalidImageError(PipelineError):
    code = "INVALID_IMAGE"
    status = 400
    message = "사진을 읽을 수 없어요. 다시 촬영해 주세요."


class MissingApiKeyError(PipelineError):
    """GEMINI_API_KEY 가 없다 — 설정 실수이지 Gemini 의 실패가 아니다.

    재시도해도 절대 성공하지 않으므로 즉시 올린다. 예전에는 이 경우가 3회 재시도를
    돌고 GEMINI_EMPTY("이미지 생성에 실패했어요")로 나갔는데, 그러면 .env 를 빠뜨린
    사람이 엉뚱한 곳을 뒤지게 된다.
    """

    code = "MISSING_API_KEY"
    status = 503
    message = "서버에 AI 키가 설정되지 않았어요."


class QuotaExhaustedError(PipelineError):
    """Gemini 크레딧 소진. 재시도해도 소용없으므로 즉시 올린다."""

    code = "QUOTA_EXHAUSTED"
    status = 429
    message = "이미지 생성 한도를 초과했어요."


class GeminiEmptyError(PipelineError):
    """재시도를 다 쓰고도 이미지가 안 왔다.

    Gemini 가 간헐적으로 응답 parts 를 비워서 준다(실측 6회 중 1회).
    pipeline 이 3회 재시도한 뒤에도 실패했을 때만 여기 온다.
    """

    code = "GEMINI_EMPTY"
    status = 502
    message = "이미지 생성에 실패했어요. 다시 시도해 주세요."


class SpriteQualityError(PipelineError):
    """누끼 결과가 쓸 수 없는 상태다.

    투명 비율로 판정한다. 정상은 65~70% 인데,
      - 0% 에 가까우면: 배경이 충분히 희지 않아 가장자리 흰 덩어리가 안 잡힌 것.
        그대로 두면 AR 화면에 불투명한 네모 판이 뜬다.
      - 95% 에 가까우면: 캐릭터까지 배경으로 판정된 것(흰색·크림색 인형, R8).
    둘 다 200 으로 나가면 아무도 실패를 모른 채 앱에서 깨진 그림을 보게 된다.

    자동 재시도는 하지 않는다. 흰 인형처럼 원인이 구조적이면 재시도해도 매번
    실패하면서 호출 비용만 3배로 든다.
    """

    code = "SPRITE_INVALID"
    status = 502
    message = "인형을 잘 오려내지 못했어요. 밝고 단색인 배경에서 다시 찍어주세요."


class GeminiTimeoutError(PipelineError):
    code = "GEMINI_TIMEOUT"
    status = 504
    message = "이미지 생성이 너무 오래 걸려요. 다시 시도해 주세요."


# ---------------------------------------------------------------------------
# 대화(WS /doll/talk) 전용
#
# ⚠️ WebSocket 은 main.py 의 @app.exception_handler(PipelineError) 를 **타지 않는다.**
#    핸드셰이크가 끝난 뒤에는 HTTP 응답을 만들 수 없기 때문이다. 그래서 이 예외들은
#    routes/talk.py 가 직접 잡아서 `{"type":"error","code":...}` 프레임 + close code
#    로 바꾼다. status 는 그 변환표를 사람이 읽기 쉬우라고 남겨 둔 값이다.
# ---------------------------------------------------------------------------
class TalkBusyError(PipelineError):
    """동시 대화 세션 한도 초과.

    stylize 처럼 큐에 세워 두면 안 된다 — 앞선 대화가 밥 한 끼(18분) 동안 안 끝나기
    때문에, 기다리게 하면 앱은 응답 없는 연결을 붙들고 있게 된다. 즉시 거절해서
    앱이 "지금은 안 된다"를 알게 하는 편이 낫다. WS close code 는 1013(try again later).
    """

    code = "TALK_BUSY"
    status = 503
    message = "지금은 친구가 다른 아이와 이야기 중이에요. 잠시 후 다시 불러주세요."


class LiveUnavailableError(PipelineError):
    """Gemini Live 에 연결하지 못했다(재연결도 모두 실패).

    크레딧 소진·모델명 오류·네트워크 단절이 모두 여기로 온다. 앱은 대화를 끝내고
    MainHome 으로 돌아가면 된다.
    """

    code = "LIVE_UNAVAILABLE"
    status = 502
    message = "친구와 연결하지 못했어요. 잠시 후 다시 시도해 주세요."
