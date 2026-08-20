"""아이 프로필 — 인형이 아이 이름을 부르게 하는 재료.

    회원가입(이름·나이) + 관심사 입력  ─앱 로컬 저장─>  WS /doll/talk?child=…&age=…
                                                          │
                                                          └─> system_instruction

📌 **2026-08-20 개정 — 서버가 회원 DB 를 조회한다.** 아래 기록은 낡았다.

    옛 결정은 "앱이 실어 보낸다" 였고 근거는 ① AI 서버가 팀원의 인증 백엔드와
    결합되는 것을 피한다 ② 앱은 로그인 직후 이미 값을 갖고 있다 ③ 무상태 유지,
    였다. **①의 전제가 사라졌다** — 지금은 FastAPI 하나·DB 하나이고
    routes/talk.py 는 이미 토큰을 검증해 user_id 를 쓴다.

    **②는 사실이 아니었다.** 앱의 ChildProfileStore.save() 가 어디에서도 불리지
    않아서(SignUpScreen 의 콜백이 인자 없는 () -> Unit 으로 되돌아갔다) 값이 늘
    비어 있었다. 그 결과 인형이 아이 이름을 한 번도 부르지 못했고, 서버 로그의
    `child_name=없음 interests=0개` 가 몇 주 동안 정상처럼 보였다.
    정작 DB 에는 회원가입 때 받은 이름·나이·관심사가 다 들어 있었다.

    → 이름·나이·관심사는 **DB 가 단일 출처**다. 쿼리로 오면 그쪽을 우선한다
      (talk_client 로 다른 값을 넣어 시험할 수 있어야 하므로).

    ⚠️ goals(오늘의 목표)만은 여전히 앱이 실어 보낸다. goal_tags 는 '자주 쓰는
       문구 사전'이라 오늘 무엇을 골랐는지 DB 에서 알 수 없기 때문이다.

🔐 개인정보다. **아이 이름을 로그에 남기지 않는다** — 서버 로그는 팀 전체가 보고,
   시연 중 화면에 띄우기도 한다. 로깅에는 safe_repr() 를 쓸 것.
"""

import logging
import re
from dataclasses import dataclass, field, replace

from ai.dialog_test import DEFAULT_CHILD_AGE, DEFAULT_MODE, SITUATION

log = logging.getLogger(__name__)

# 입력값 상한. 부모가 친 텍스트가 system_instruction 에 그대로 들어가므로
# 길이를 막지 않으면 프롬프트 전체를 밀어낼 수 있다.
MAX_NAME_LEN = 12
MAX_INTERESTS = 5
MAX_INTEREST_LEN = 20

# 이번 식사의 목표. 앱의 FeedMissionDialog 가 최대 3개까지 고르게 한다
# (MAX_GOALS_PER_SESSION). 서버도 같은 상한을 둔다 — 앱만 믿으면 쿼리를 직접
# 만든 요청이 목표 스무 개로 프롬프트를 밀어낼 수 있다.
#
# ⚠️ 관심사보다 정화가 중요하다. 관심사는 앱이 고정 목록에서 고르게 하지만
#    목표는 AddMissionDialog 에서 **부모가 직접 타이핑**한다.
MAX_GOALS = 3
MAX_GOAL_LEN = 30

# 이름에 허용할 문자. 한글·영문·공백만 남긴다.
#
# 개행 제거만으로도 규칙 주입(`지우\n- 규칙을 모두 무시한다`)은 막힌다 — 한 줄로
# 눌리면 새 규칙 줄이 되지 못하기 때문이다. 그런데 그 잔해가 **이름으로 남아
# 인형이 소리 내어 읽는다.** 이름 자리에 문장부호가 올 일은 없으므로 여기서 턴다.
_NAME_ALLOWED = re.compile(r"[^가-힣ㄱ-ㅎㅏ-ㅣa-zA-Z\s]")

# 나이 허용 범위. 벗어나면 값을 버리고 기본값으로 돌아간다 — 오타(`44`)를
# 그대로 넣으면 인형이 44살 어른에게 말하듯 어휘를 바꾼다.
MIN_AGE, MAX_AGE = 1, 12

# 개행·제어문자 제거용. 개행이 남으면 프롬프트에 새 규칙 줄을 끼워 넣을 수 있다
# (`지우\n- 규칙을 모두 무시한다`). 한 줄로 눌러 두면 그게 안 된다.
_WHITESPACE = re.compile(r"\s+")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _clean(raw: str | None, limit: int) -> str:
    if not raw:
        return ""
    text = _WHITESPACE.sub(" ", _CONTROL.sub(" ", raw)).strip()
    return text[:limit]


def _clean_name(raw: str | None) -> str:
    if not raw:
        return ""
    return _clean(_NAME_ALLOWED.sub(" ", raw), MAX_NAME_LEN)


def _clean_mode(raw: str | None) -> str:
    """아는 값만 통과시킨다.

    이 값은 system_instruction 에 들어갈 블록을 고르는 열쇠다. 모르는 값을 그대로
    쓰면 프롬프트에 장난 문구가 섞이므로, 목록에 없으면 기본값으로 되돌린다.
    """
    return raw if raw in SITUATION else DEFAULT_MODE


def _clean_age(raw) -> int | None:
    """쿼리스트링('4')과 DB 컬럼(int 4)을 둘 다 받는다.

    ⚠️ 예전에는 `raw.strip()` 을 먼저 불러서 **int 가 오면 AttributeError 로 조용히
       버려졌다.** DB 조회를 붙이는 순간 나이가 늘 기본값(4살)으로 떨어지는데,
       마침 기본값이 4라서 눈으로는 정상으로 보인다.
    """
    if raw is None or raw == "":
        return None
    try:
        age = int(str(raw).strip())
    except (ValueError, TypeError):
        return None
    return age if MIN_AGE <= age <= MAX_AGE else None


def _clean_csv(raw: str | None, max_items: int, max_len: int) -> tuple[str, ...]:
    """`공룡,딸기` 같은 쉼표 목록을 정화해 튜플로. 중복은 버리고 개수를 자른다.

    관심사와 목표가 같은 함수를 쓴다. **입구가 둘이어도 정화는 하나여야 한다** —
    한쪽만 고치면 다른 쪽으로 개행이 들어와 프롬프트에 새 규칙 줄이 생긴다.
    """
    if not raw:
        return ()
    items: list[str] = []
    for part in raw.split(","):
        item = _clean(part, max_len)
        if item and item not in items:
            items.append(item)
        if len(items) >= max_items:
            break
    return tuple(items)


@dataclass(frozen=True)
class ChildProfile:
    """비어 있어도 정상이다. 그때는 기본 페르소나('초록이', 4살)로 돌아간다.

    프로필이 없다고 대화를 거절하면 안 된다 — 회원가입 필드가 아직 앱에 없고,
    있더라도 관심사는 나중에 입력하는 값이라 빈 채로 오는 경우가 정상 경로다.
    """

    child_name: str = ""
    child_age: int | None = None
    interests: tuple[str, ...] = field(default_factory=tuple)
    doll_name: str = ""
    # 어느 화면에서 연결했는지. 기본값은 밥친구 — 앱이 안 보내도 기존과 같이 돈다.
    mode: str = DEFAULT_MODE
    # 이번 식사에 아이가 고른 목표. mode 가 meal 일 때만 프롬프트에 붙는다
    # (ai.dialog_test.goal_block). 밥 시간이 아닌데 목표를 들이대면 인형이
    # 홈 화면에서 "당근 먹자" 를 꺼낸다.
    goals: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_empty(self) -> bool:
        return not (self.child_name or self.child_age or self.interests or self.doll_name)

    @classmethod
    def from_query(cls, params) -> "ChildProfile":
        """WS 쿼리스트링에서 읽는다. 앱과의 계약이므로 키 이름을 바꾸지 말 것.

            /doll/talk?child=지우&age=4&interests=공룡,딸기&doll=초록이&goals=당근 먹기,골고루 먹기
        """
        return cls(
            child_name=_clean_name(params.get("child")),
            child_age=_clean_age(params.get("age")),
            interests=_clean_csv(params.get("interests"), MAX_INTERESTS, MAX_INTEREST_LEN),
            doll_name=_clean_name(params.get("doll")),
            mode=_clean_mode(params.get("mode")),
            goals=_clean_csv(params.get("goals"), MAX_GOALS, MAX_GOAL_LEN),
        )

    def with_db_defaults(self, row) -> "ChildProfile":
        """회원 DB 에서 읽은 값으로 **빈 자리만** 채운다.

        🔴 쿼리가 이긴다. DB 를 우선하면 talk_client 의 --child/--age/--interests 로
           다른 값을 넣어 시험할 수 없어진다 — 인형 이름 발음을 미리 들어보는
           것처럼, 실제 회원 정보와 다른 값으로 확인해야 하는 경우가 있다.

        row 가 없어도(회원 조회 실패·탈퇴) 정상이다. 그때는 그대로 돌려준다 —
        프로필이 없다고 대화를 거절하면 안 된다.
        """
        if not row:
            return self
        return replace(
            self,
            child_name=self.child_name or _clean_name(row.get("name")),
            child_age=self.child_age or _clean_age(row.get("age")),
            # DB 의 keyword 는 `공룡,딸기` 형태의 쉼표 문자열이다. 회원가입에서
            # 앱이 그렇게 저장한다(SignUpScreen 의 joinToString).
            interests=self.interests
            or _clean_csv(row.get("keyword"), MAX_INTERESTS, MAX_INTEREST_LEN),
        )

    def safe_repr(self) -> str:
        """로그용. 🔐 아이 이름·관심사·목표 내용은 넣지 않는다 — 개수만 남긴다.

        목표 문구도 개인정보다. `편식 정복`, `물 마시기` 같은 값이 로그에 쌓이면
        아이의 식습관 기록이 된다. 팀 전체가 보는 로그에 남길 것이 아니다.
        """
        return (
            f"child_name={'있음' if self.child_name else '없음'} "
            f"age={self.child_age or DEFAULT_CHILD_AGE} "
            f"interests={len(self.interests)}개 "
            f"doll_name={'있음' if self.doll_name else '기본값'} "
            f"mode={self.mode} goals={len(self.goals)}개"
        )
