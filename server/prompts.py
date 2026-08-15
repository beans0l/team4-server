"""프롬프트 로딩 — 앱에 실려 출시되는 산출물.

**프롬프트 문자열을 이 파일에 복사해 두지 않는다.** 기본값의 단일 출처는
ai/doll_stylize_test.py 의 ACTIVE_PROMPT 다. 검증 스크립트도 같은 이름을 실행하므로
스크립트로 확인한 결과가 곧 앱에 나가는 결과다.

    ⚠️ 실제로 갈라진 적이 있다. 스크립트는 STYLE_PROMPT(v1)를 돌리는데 서버는
       PROMPT_V3 를 쓰고 있었고, PROMPT_V3 는 스크립트에서 호출되지 않는 죽은
       코드였다. 그 상태로 튜닝하면 확인한 것과 나가는 것이 다르다.

우선순위:
    ① 환경변수 STYLIZE_PROMPT      이미지 재빌드 없이 교체 (.env 고치고 재시작)
    ② server/prompts/stylize.txt   파일만 바꾸면 됨 (코드 리뷰 불필요)
    ③ ACTIVE_PROMPT                기본값 (= 현재 PROMPT_V3)

①② 를 둔 이유:
    이 서버는 팀원 저장소에 있지만 프롬프트는 AI 파트가 거의 매일 고친다.
    코드에 박아 두면 한 줄 고칠 때마다 팀원을 경유해야 해서 병목이 된다.

배경 지정에 대해:
    Gemini 는 알파 채널을 만들지 못한다(투명 요구해도 3/3 RGB). 대신 "스타일 변환과
    동시에" 순백 배경을 요구하면 모서리 RGB 254.7±0.6 으로 4/4 재현된다.
    그래서 프롬프트의 `plain solid white background` 는 장식이 아니라 뒤따르는
    코드 누끼(cutout)의 전제 조건이다. 빼면 누끼가 실패한다.
"""

import json
import logging

from ai.dialog_test import DOLL_PERSONA as DEFAULT_PERSONA
from ai.doll_stylize_test import ACTIVE_PROMPT
from ai.sprite_test import OPTIONAL_SPRITE_PROMPTS, SPRITE_PROMPTS
from server import config

log = logging.getLogger(__name__)


def load_stylize_prompt() -> tuple[str, str]:
    """(프롬프트, 출처) 를 돌려준다. 출처는 로그·헬스체크로 노출한다."""
    if config.STYLIZE_PROMPT_ENV:
        return config.STYLIZE_PROMPT_ENV, "env:STYLIZE_PROMPT"

    path = config.STYLIZE_PROMPT_FILE
    if path.is_file():
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text, f"file:{path.name}"
        log.warning("%s 가 비어 있어 기본 프롬프트를 쓴다", path)

    return ACTIVE_PROMPT, "default:ACTIVE_PROMPT"


def _default_sprite_prompts() -> dict[str, str]:
    """R1 을 검증한 4장. SPRITE_BACK 이 켜져 있으면 뒷모습을 얹는다."""
    prompts = dict(SPRITE_PROMPTS)
    if config.SPRITE_BACK:
        prompts.update(OPTIONAL_SPRITE_PROMPTS)
        log.warning(
            "SPRITE_BACK 켜짐 — 'back' 은 R1 지표로 검증되지 않은 스프라이트다"
        )
    return prompts


def load_sprite_prompts() -> tuple[dict[str, str], str]:
    """변형 스프라이트 프롬프트. ({이름: 프롬프트}, 출처).

    base(mouth_closed)는 여기 없다 — 그건 STYLIZE_PROMPT 로 만들고, 그 결과를
    입력으로 이 프롬프트들을 돌린다(체이닝). 원본 사진에서 5장을 각각 뽑으면
    자세부터 달라진다. 이미 정리된 일러스트를 주면 해석의 여지가 사라진다.

    ⚠️ 프롬프트를 이 파일에 복사하지 않는다. 기본값의 단일 출처는
       ai/sprite_test.py 의 SPRITE_PROMPTS 이고, `--measure` 가 재는 것도 그것이다.
       갈라지면 "정렬 IoU 99.9%" 라는 검증이 앱에 나가는 결과와 무관해진다.
    """
    raw, source = None, None
    if config.SPRITE_PROMPTS_ENV:
        raw, source = config.SPRITE_PROMPTS_ENV, "env:SPRITE_PROMPTS"
    else:
        path = config.SPRITE_PROMPTS_FILE
        if path.is_file():
            text = path.read_text(encoding="utf-8").strip()
            if text:
                raw, source = text, f"file:{path.name}"
            else:
                log.warning("%s 가 비어 있어 기본 프롬프트를 쓴다", path)

    if raw is None:
        return _default_sprite_prompts(), "default:SPRITE_PROMPTS"

    # 오버라이드가 깨져 있으면 **기본값으로 돌아간다.** 여기서 예외를 올리면
    # 오타 하나로 서버가 기동조차 못 한다 — 시연 직전에 프롬프트를 만지다
    # 그렇게 되면 되돌릴 시간이 없다.
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict) or not parsed:
            raise ValueError("최상위가 비어 있지 않은 객체여야 한다")
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()):
            raise ValueError("키와 값이 모두 문자열이어야 한다")
    except (json.JSONDecodeError, ValueError) as e:
        log.error("%s 를 읽지 못해 기본 프롬프트를 쓴다: %s", source, e)
        return _default_sprite_prompts(), "default:SPRITE_PROMPTS(오버라이드 실패)"

    return parsed, source


def load_persona() -> tuple[str, str]:
    """인형 페르소나. (프롬프트, 출처).

    Live API 는 이걸 system_instruction 으로 받는다. **빠뜨리면 페르소나가 통째로
    무시된다** — 실측에서 `저는 유치원에 다니지 않아서...` 같은 존댓말 어시스턴트
    톤이 나왔다. 반말로 친구처럼 말하는 인형이 아니라 상담원이 되는 것이다.

    기본값의 단일 출처는 ai/dialog_test.py 의 DOLL_PERSONA 다. 스타일 프롬프트와
    같은 이유로 여기에 복사하지 않는다 — 과제 4 를 재측정할 때 스크립트가 돌리는
    문장과 서버가 쓰는 문장이 같아야 실측값을 믿을 수 있다.
    """
    if config.DOLL_PERSONA_ENV:
        return config.DOLL_PERSONA_ENV, "env:DOLL_PERSONA"

    path = config.DOLL_PERSONA_FILE
    if path.is_file():
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text, f"file:{path.name}"
        log.warning("%s 가 비어 있어 기본 페르소나를 쓴다", path)

    return DEFAULT_PERSONA, "default:DOLL_PERSONA"


STYLIZE_PROMPT, STYLIZE_PROMPT_SOURCE = load_stylize_prompt()
SPRITE_PROMPT_SET, SPRITE_PROMPT_SOURCE = load_sprite_prompts()
DOLL_PERSONA, DOLL_PERSONA_SOURCE = load_persona()

# base 스프라이트의 이름. 앱과의 계약이다 — sprites[0] 이 항상 이것이다.
BASE_SPRITE = "mouth_closed"

__all__ = [
    "STYLIZE_PROMPT",
    "STYLIZE_PROMPT_SOURCE",
    "load_stylize_prompt",
    "SPRITE_PROMPT_SET",
    "SPRITE_PROMPT_SOURCE",
    "load_sprite_prompts",
    "BASE_SPRITE",
    "DOLL_PERSONA",
    "DOLL_PERSONA_SOURCE",
    "load_persona",
]
