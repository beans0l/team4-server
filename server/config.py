"""환경변수 설정.

로컬에서는 .env 파일을 읽고, 배포에서는 서버가 주입한 진짜 환경변수를 읽는다.
load_env() 는 파일이 없으면 조용히 넘어가므로 두 환경에서 같은 코드가 돈다.
"""

import os
from pathlib import Path

from ai.doll_stylize_test import DEFAULT_BG_MODEL, load_env

load_env()

ROOT = Path(__file__).resolve().parent


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    """'0'/'false'/'no' 를 거짓으로 본다.

    ⚠️ bool(os.environ.get(...)) 로 쓰면 안 된다. 문자열 "0" 이 참이라서
       SPRITE_SET=0 으로 꺼놓은 줄 알았는데 5장이 나가고 비용이 5배 든다.
    """
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


# --- Gemini ---------------------------------------------------------------
# ai/doll_stylize_test.py 의 _gemini_client() 가 둘 중 아무거나 받아들이므로
# 존재 확인도 같은 기준으로 한다.
GEMINI_API_KEY = (
    os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""
).strip()

# Flash 확정. Pro 는 "더 잘 그리려고" 해석을 더 해서 정체성을 훼손한다(실측).
# 색이 파스텔로 밝아지고 없는 이목구비를 그려 넣는다. 비싼 모델로 바꾸지 말 것.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-image")

# 빈 parts 응답이 6회 중 1회 발생한다. 3회면 실패율이 0.5% 아래로 떨어진다.
GEMINI_ATTEMPTS = _int("GEMINI_ATTEMPTS", 3)

# --- 대화 (Live API) ------------------------------------------------------
# 음성↔음성 단일 연결. 과제 4 실측에서 TTFB 0.85초로, 3단 조립(STT→LLM→TTS,
# 2.71~5.38초)보다 3배 빨라 채택했다. 아이는 1초 넘는 침묵을 "인형이 죽었다"로
# 받아들이므로 이 격차가 결정적이다.
# ⚠️ REST 로는 대체 불가능하다. 모델을 바꾸려면 Live 를 지원하는 모델이어야 한다.
LIVE_MODEL = os.environ.get("LIVE_MODEL", "gemini-3.1-flash-live-preview")

# 성대는 Google 제공 30개 중 택1 이고 음색 조절·커스텀 학습은 불가능하다.
# 실제 커스터마이징 레버는 페르소나 프롬프트 쪽이다(prompts.py 참조).
# 인형을 여러 개 등록하면 인형마다 다른 이름을 배정해 목소리를 구분할 수 있다.
DOLL_VOICE = os.environ.get("DOLL_VOICE", "Leda")

# 동시 대화 세션 수. 한 세션이 밥 한 끼(약 18분) 내내 연결을 붙들고 있고
# Live 는 오디오를 상시 스트리밍하므로, stylize 와 달리 **크레딧**이 병목이다.
# 🔴 R5(Live 비용)가 아직 미측정이다. 실측 전에는 올리지 말 것.
MAX_TALK_SESSIONS = _int("MAX_TALK_SESSIONS", 2)

# Live 연결이 끊겼을 때 재연결 시도 횟수. 앱의 WS 는 유지하고 맥락만 초기화한다.
LIVE_RECONNECT_ATTEMPTS = _int("LIVE_RECONNECT_ATTEMPTS", 3)

# 🔴 앱의 첫 프레임을 읽기 전에 Live 연결이 서기를 기다리는 시간.
#    연결은 즉시 되지 않는다(실측 약 0.5초). 기다리지 않고 바로 읽으면 그 사이에
#    도착한 activity_start 를 버리게 되고, 그러면 Gemini 는 발화가 시작된 줄 모른 채
#    activity_end 만 받아 **아무 응답도 하지 않는다.** 증상이 "35초 무응답"이라
#    원인이 연결 타이밍이라는 걸 알아채기 어렵다(실제로 겪었다).
#    재연결(최대 3회, 백오프 합 3.5초)까지 감안해 넉넉히 잡는다.
LIVE_CONNECT_TIMEOUT_SEC = _int("LIVE_CONNECT_TIMEOUT_SEC", 20)

# 재연결 도중 들어온 activity 신호를 얼마나 기다렸다가 버릴지.
# 오디오 한 조각은 없어도 말이 되지만, 턴의 시작·끝 표시가 사라지면 그 턴 전체가
# 무응답이 된다. 그래서 신호에만 짧은 유예를 준다.
LIVE_SIGNAL_WAIT_SEC = _int("LIVE_SIGNAL_WAIT_SEC", 3)

# 오디오 형식. 앱과의 계약이라 바꾸려면 앱 파트와 합의해야 한다.
LIVE_INPUT_RATE = 16000  # 업: 앱 -> 서버 -> Gemini
LIVE_OUTPUT_RATE = 24000  # 다운: Gemini -> 서버 -> 앱

# --- 배경 제거 ------------------------------------------------------------
# isnet-general-use = 1.2초. 이전 기본값(birefnet-general)은 32~49초였고
# 알파 마스크는 99.3% 일치한다. 근거는 ai/doll_stylize_test.py 의 표 참조.
REMBG_MODEL = os.environ.get("REMBG_MODEL", DEFAULT_BG_MODEL)

# --- 이미지 ---------------------------------------------------------------
MAX_UPLOAD_BYTES = _int("MAX_UPLOAD_BYTES", 10 * 1024 * 1024)

# 긴 변 기준. 원본(4.2MB, 4284x5712)을 그대로 넣으면 전송·디코딩이 길어진다.
# ⚠️ 배경 제거 속도와는 무관하다 — rembg 는 내부에서 고정 크기로 리사이즈하므로
#    입력을 512px 로 줄여도 추론이 빨라지지 않는다(실측: 512px 이 오히려 느림).
MAX_EDGE_PX = _int("MAX_EDGE_PX", 1024)

# --- 스프라이트 세트 --------------------------------------------------------
# 립싱크·표정용 5장을 만든다: mouth_closed(=base) / mouth_half / mouth_open
#                              / happy / sleepy
# 입 3장이 초당 20~30회 교체돼야 인형이 말하는 것처럼 보인다. 1장만으로는
# 소리만 나고 입이 안 움직인다.
#
# 🔴 끄는 스위치를 남겨둔 이유는 **비용**이다. 5장이면 등록 1회에 325원이 든다.
#    목 모드가 없어서 개발 중 호출도 전부 과금되므로, 앱 배선을 반복 테스트할
#    때는 SPRITE_SET=0 으로 1장(65원)만 만들어 쓴다. 응답 형식은 그대로다.
SPRITE_SET = _bool("SPRITE_SET", True)

# 뒷모습 추가(6장째). 기본은 꺼둔다 — R1 지표로 검증할 수 없고(실루엣이 달라야
# 정상이라 정렬 IoU 를 못 쓴다) 없어도 Z축 제자리 스핀으로 대체되기 때문이다.
SPRITE_BACK = _bool("SPRITE_BACK", False)

# 변형 스프라이트 동시 생성 수. 순차로 하면 5장에 54초가 걸린다(실측).
# 네트워크 대기라 CPU 를 안 쓰므로 MAX_CONCURRENCY 와는 별개로 둔다.
SPRITE_PARALLEL = _int("SPRITE_PARALLEL", 4)

# --- 실행 -----------------------------------------------------------------
# 파이프라인 전체 예산.
# ⚠️ 스프라이트 세트를 켜면 정상 소요가 11.5초에서 약 28초로 늘어난다. 거기에
#    Gemini 재시도(장당 최대 3회)가 겹치면 훨씬 길어지므로 예산을 2배로 잡는다.
#    이 값을 줄이려면 nginx proxy_read_timeout 도 같이 봐야 한다(README 배포 ④).
PIPELINE_TIMEOUT_SEC = _int("PIPELINE_TIMEOUT_SEC", 180)

# 동시 실행 수. 요청 1건이 최대 약 1GB 를 쓴다(실측 1,036MB).
#   메모리 2GB 이하 -> 1 / 4GB(가비아) -> 2
# 늘릴 때는 서버 메모리를 먼저 확인할 것. 넘치면 OOM 으로 프로세스가 죽는다.
MAX_CONCURRENCY = _int("MAX_CONCURRENCY", 2)

# --- 프롬프트 -------------------------------------------------------------
# 우선순위: 환경변수 > 파일 > ai/doll_stylize_test.py 의 PROMPT_V3
# 프롬프트는 AI 파트가 계속 튜닝하는 산출물이라 코드 수정 없이 갈아끼울 수 있어야 한다.
STYLIZE_PROMPT_ENV = os.environ.get("STYLIZE_PROMPT", "").strip()
STYLIZE_PROMPT_FILE = Path(
    os.environ.get("STYLIZE_PROMPT_FILE", ROOT / "prompts" / "stylize.txt")
)

# 변형 스프라이트 프롬프트도 같은 3단. 파일은 {이름: 프롬프트} JSON 이다.
# 기본값은 ai/sprite_test.py 의 SPRITE_PROMPTS — R1 을 검증한 바로 그 문장들이다.
SPRITE_PROMPTS_ENV = os.environ.get("SPRITE_PROMPTS", "").strip()
SPRITE_PROMPTS_FILE = Path(
    os.environ.get("SPRITE_PROMPTS_FILE", ROOT / "prompts" / "sprites.json")
)

# 인형 페르소나도 같은 3단으로 둔다. R7(Live 응답이 너무 길다)이 미해소라
# 시연 직전까지 문구를 조일 물건이고, 코드에 박으면 매번 재배포해야 한다.
DOLL_PERSONA_ENV = os.environ.get("DOLL_PERSONA", "").strip()
DOLL_PERSONA_FILE = Path(
    os.environ.get("DOLL_PERSONA_FILE", ROOT / "prompts" / "persona.txt")
)

# --- 저장소 ---------------------------------------------------------------
# 결과 PNG 는 서버 디스크에 쓰고 /files 로 직접 서빙한다.
# 가비아 VM 은 디스크가 영속이라 이게 정상 운영 방식이다.
#
# ⚠️ 기본값 /tmp 는 개발용이다. 배포에서는 반드시 다른 경로로 바꿀 것 —
#    리눅스는 /tmp 를 주기적으로 비우고(systemd-tmpfiles), 도커 컨테이너 안의
#    /tmp 는 컨테이너를 다시 만들면 사라진다. 둘 다 등록된 인형이 통째로
#    없어지는 결과가 되고, 앱에는 깨진 이미지만 남는다.
#    도커로 띄운다면 이 경로에 볼륨(-v)도 같이 마운트할 것.
LOCAL_STORAGE_DIR = os.environ.get("LOCAL_STORAGE_DIR", "/tmp/doll-sprites")
