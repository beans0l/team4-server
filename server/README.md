# Doll AI Server

인형 사진을 유아용 2D 캐릭터 **투명 PNG** 로 바꿔주는 FastAPI 서버.

```
Android ──사진──> 이 서버 ──> Gemini ──> 투명 PNG URL
```

Gemini API 키는 **이 서버에만** 존재한다. 앱에 키를 넣으면 APK 를 뜯어서 꺼낼 수 있고,
크레딧이 $10 뿐이라 유출되면 시연 전에 소진된다.

---

## 기능 추가하기

`routes/` 에 새 모듈을 만들고 `main.py` 에서 `include_router()` 하면 된다.
FastAPI 의 표준 구조라, 파일이 갈려 있어서 여럿이 같이 작업해도 충돌이 잘 안 난다.

```python
# routes/friends.py
router = APIRouter()

@router.post("/api/friends")
async def create_friend(...): ...

# main.py
from server.routes import friends
app.include_router(friends.router)
```

CRUD·DB 도 같은 방식으로 붙인다.

**바꾸기 전에 확인할 것** — 이유가 있어서 그렇게 돼 있는 것들이다:

| 파일 | 주의 |
|---|---|
| `errors.py` | 앱과의 **연동 계약**. `code` 문자열을 바꾸면 앱 분기가 깨진다 |
| `prompts.py`, `../ai/` | 프롬프트의 **단일 출처**. 복사해 두면 검증한 것과 나가는 것이 갈라진다 |
| `pipeline.py` | 단계를 빼면 안 된다(특히 사전 누끼). 이유는 아래 파이프라인 절 |
| `config.py` | 기본값마다 실측 근거가 주석에 있다 |
| `profile.py` | 아이 이름·나이는 **개인정보**다. 로그에는 `safe_repr()` 만 쓸 것 |
| `live.py` | 페르소나는 인스턴스에 보관한다. 지역변수로 바꾸면 **재연결 후 아이 이름을 잊는다** |

---

## 시작하기

저장소 루트에 `.env` 를 만들고 키를 넣는다 (`.env` 는 커밋 금지).

```
GEMINI_API_KEY=AIza...
```

```bash
pip install -r ../requirements.txt

uvicorn server.main:app --reload --port 8000
curl -F image=@어떤사진.jpg localhost:8000/doll/stylize
```

**키가 없으면 모든 변환 요청이 503 `MISSING_API_KEY` 로 실패한다.** 기동 로그에도 에러가 뜬다.
가짜로 성공하는 경로는 없다 — 모든 요청이 실제 Gemini 로 가고, **개발·리허설 중에도
호출 비용이 그대로 든다**(등록 1회 약 65원).

첫 기동은 배경 제거 모델(수백 MB)을 내려받느라 오래 걸릴 수 있다. 워밍업이 이걸 미리 한다.

---

## ⚠️ 소요 시간과 타임아웃 — 가장 흔한 함정

**이 API 는 느리다.** 기본값으로 두면 클라이언트가 먼저 끊어버린다.

| | 5장 (기본) | 1장 (`SPRITE_SET=0`) |
|---|---|---|
| 보통 | **약 28초** | 약 12초 |
| 최악 | Gemini 재시도가 겹치면 훨씬 길어진다 | 약 35초 |
| 서버 예산 | 180초 (`PIPELINE_TIMEOUT_SEC`) | 〃 |

> **클라이언트 타임아웃은 120초로 잡을 것.**
> Android(OkHttp)라면 `readTimeout(120, SECONDS)` 를 반드시 설정한다. 기본값 10초로는 100% 실패한다.
> `DollLoading` 모달이 이 시간을 덮는다.

base 10초 + 변형 4장 병렬 13초 ≈ 28초다. 변형을 순차로 하면 54초가 된다(실측).

앞단에 nginx 를 두면 **거기도 같이 늘려야 한다.** `proxy_read_timeout` 기본값(60초)이
서버 예산 180초보다 작아서, 오래 걸리는 요청에서 nginx 가 먼저 끊고 앱은 우리 JSON 대신
**HTML 504** 를 받는다 (배포 절 ④ 참조).

재시도가 필요한 이유: Gemini 가 **6회 중 1회꼴로 빈 응답**을 준다(실측).
서버가 내부에서 3회 재시도하므로 **호출하는 쪽은 재시도하지 말 것** — 중복되면 대기가 2분을 넘는다.

> **대화(`WS /doll/talk`)에는 위 숫자가 하나도 적용되지 않는다.** 저쪽은 연결을 18분간
> 열어두고, 응답은 아이가 말을 마친 뒤 1초 안에 온다. OkHttp 를 쓴다면 WebSocket 은
> `readTimeout` 을 **0(무제한)** 으로 둬야 한다 — 60초로 두면 아이가 1분만 조용히 밥을
> 먹어도 연결이 끊긴다. nginx 쪽도 마찬가지다(배포 절 ④).

---

## 엔드포인트

### `POST /doll/stylize`

> 🔴 **로그인 필요.** `Authorization: Bearer <JWT>` 없이 부르면 401 `UNAUTHORIZED`.
> 호출 1건이 최대 325원이라, 익명으로 열어두면 아무나 크레딧을 쓸 수 있었다.

```
Authorization: Bearer <로그인 때 받은 토큰>
Content-Type: multipart/form-data
  image = 촬영 jpg/png (10MB 이하)
```

**성공 200** — 립싱크·표정용 **5장**을 만든다.

```json
{
  "sprites": ["https://.../files/c9b32cb8fc4d_mouth_closed.png", "...half", "...open",
              "...happy", "...sleepy"],
  "sprite_map": {
    "mouth_closed": "https://.../c9b32cb8fc4d_mouth_closed.png",
    "mouth_half":   "https://.../..._mouth_half.png",
    "mouth_open":   "https://.../..._mouth_open.png",
    "happy":        "https://.../..._happy.png",
    "sleepy":       "https://.../..._sleepy.png"
  },
  "failed": [],
  "elapsed_ms": 28140,
  "timing": { "load": 136, "rembg": 1453, "gemini_base": 12102,
              "gemini_variants": 13400, "total": 28140, "transparent_pct": 68.0 }
}
```

| 스프라이트 | 쓰임 |
|---|---|
| `mouth_closed` | 기본. **`sprites[0]` 은 항상 이것이다** |
| `mouth_half` / `mouth_open` | 립싱크 — 오디오 RMS 에 맞춰 초당 20~30회 교체 |
| `happy` / `sleepy` | 표정 — 상태에 따라 교체 |

- **`sprite_map` 을 쓸 것.** 립싱크는 이름으로 골라야 한다. `sprites` 배열은 1장만 쓰던
  기존 클라이언트를 위해 유지하는 것이고, `[0]` 이 base 라는 보장만 한다.
- **`failed` 가 비어 있지 않으면** 그 스프라이트만 없는 것이다. 앱은 **립싱크를 끄고
  `mouth_closed` 정지 이미지로** 동작하면 된다. 등록 자체는 성공이다.
- 립싱크 임계값(정규화 RMS): `<0.08` closed / `<0.30` half / 나머지 open.
- `timing` 은 디버깅용 부가 정보라 무시해도 된다.

**왜 부분 실패를 200 으로 내보내는가**: 변형 하나 때문에 등록 전체를 실패시키면 아이는
28초를 기다린 뒤 아무것도 못 얻는다. 립싱크가 빠져도 인형은 나온다. 단 조용히 빠지면
안 되므로 `failed` 에 이름을 담고 서버 로그에도 남긴다.

> **`SPRITE_SET=0` 으로 띄우면 1장만 만든다**(65원). 응답 형식은 그대로이고
> `sprite_map` 에 `mouth_closed` 하나만 들어온다. 앱 배선을 반복 테스트할 때 쓴다.
> **이 상태로 시연하면 인형 입이 움직이지 않는다** — `/healthz` 의 `sprite_count` 로 확인할 것.

**실패**

```json
{ "code": "GEMINI_EMPTY", "message": "이미지 생성에 실패했어요. 다시 시도해 주세요." }
```

| HTTP | `code` | 뜻 | 앱에서 |
|---|---|---|---|
| 401 | `UNAUTHORIZED` | 토큰 없음·만료·위조 | 로그인 화면으로 |
| 400 | `INVALID_IMAGE` | 디코딩 실패 · 용량 초과 | "다시 촬영해 주세요" |
| 429 | `QUOTA_EXHAUSTED` | **크레딧 소진** | 앱엔 숨기고 일반 오류로 |
| 503 | `MISSING_API_KEY` | 서버에 키가 없음 — **설정 실수** | 서버의 `.env` 확인 |
| 502 | `GEMINI_EMPTY` | 3회 재시도 후에도 실패 | 재시도 버튼 |
| 502 | `SPRITE_INVALID` | 누끼 결과가 못 쓸 상태(전부 불투명/전부 투명) | "밝고 단색 배경에서 다시 촬영" |
| 504 | `GEMINI_TIMEOUT` | 서버 예산 초과 | 재시도 버튼 |

**모든 실패는 이 형식으로 나간다.** 필드명을 틀리게 보내도 FastAPI 기본 422(`detail`)가
아니라 400 `INVALID_IMAGE` 로 변환해서 준다 — 호출하는 쪽이 항상 `code` 만 보면 되게 하려는 것.

### `WS /doll/talk` — 아이와 인형의 실시간 음성 대화

```
Android ──WS──> 이 서버 ──WS──> Gemini Live API
```

**REST 로는 안 된다.** 아이가 말을 마친 뒤 인형의 첫 소리까지 걸리는 시간을 재보면
Live 는 0.85초, STT→LLM→TTS 조립은 2.71~5.38초다. 아이는 1초 넘는 침묵을
"인형이 죽었다"로 받아들이므로 이 격차가 결정적이다.

**연결 URL**

```
wss://<도메인>/doll/talk?token=<로그인 JWT>&child=지우&age=4&interests=공룡,딸기&doll=초록이
```

🔴 **로그인 필요.** `token` 이 없거나 유효하지 않으면 즉시 거절한다(아래 실패 표).
WS 핸드셰이크는 커스텀 헤더를 못 싣는 클라이언트가 있어 `Authorization` 헤더가 아니라
쿼리파라미터로 받는다 — HTTP 라우트들과 다른 방식이니 주의할 것.
`child`/`age`/`interests`/`doll`은 그대로 전부 선택값이다.

세션이 끝나면 user_id·인형이 한 말(턴 단위)·시작/종료 시각이 `talk_sessions` 테이블에
남는다. 아이 쪽 음성은 텍스트로 전사되지 않으므로 저장되는 건 인형 발화뿐이다.

**프레임 규약** — 바이너리는 오디오, 텍스트는 제어다.

| 방향 | 프레임 | 내용 |
|---|---|---|
| 업 | binary | **16kHz** / 16-bit / mono PCM, 100ms(3200바이트) 청크 |
| 업 | text | `{"type":"activity_start"}` — 아이가 말하기 시작 |
| 업 | text | `{"type":"activity_end"}` — 아이가 말을 끝냄 |
| 다운 | binary | **24kHz** PCM — 그대로 재생 + RMS 로 립싱크 |
| 다운 | text | `{"type":"transcript","text":"..."}` — 인형이 한 말(조각. 이어붙일 것) |
| 다운 | text | `{"type":"safety_blocked","reason":"..."}` — 이 턴은 막혔다. **오디오가 안 온다** |
| 다운 | text | `{"type":"turn_complete"}` — 한 턴 끝 |
| 다운 | text | `{"type":"session_reset"}` — 재연결됨. **이전 대화 맥락이 사라졌다** |
| 다운 | text | `{"type":"error","code":"...","message":"..."}` |

**🔴 `safety_blocked` 를 무시하면 인형이 얼어붙는다.**

유해 콘텐츠 필터에 걸린 턴은 Gemini 가 **오디오를 한 조각도 보내지 않는다.** 앱이
아무것도 하지 않으면 아이 앞에서 인형이 조용히 멈춘 것처럼 보이는데, 아이는 1초 넘는
침묵을 "인형이 죽었다"로 받아들인다(TTFB 0.85초를 위해 Live 를 채택한 것과 같은 이유).

→ 이 프레임을 받으면 **미리 번들해 둔 폴백 대사 wav 중 하나를 랜덤 재생한다.**
   생성은 `.venv/bin/python -m ai.dialog_test --fallback` (산출물 `ai/out/fallback/`).
   `reason` 은 디버깅·집계용이며 앱은 무시해도 된다. 항상 `turn_complete` **직전**에 온다.

⚠️ 폴백은 **`DOLL_VOICE` 와 같은 목소리**여야 한다. 다르면 딴 인형이 끼어든 것처럼 들린다.
   인형마다 voice 를 다르게 배정하는 확장으로 가면 voice 별로 다시 뽑아야 한다.

> ⚠️ **업 16kHz, 다운 24kHz 로 다르다.** 같다고 가정하면 인형 목소리가 낮게 늘어진다.

**🔴 `activity_start` / `activity_end` 는 앱이 판단해서 보내야 한다.**
서버가 대신 정해줄 수 없다 — 마이크를 가진 쪽이 앱이기 때문이다. 실측에서
`audio_stream_end` 방식은 응답이 아예 오지 않았고(35초 타임아웃), 자동 VAD 는
응답 완료가 18.7초로 느렸다. 수동 신호가 **유일하게 동작한 조합**이다.

> 식사 중에는 식기·TV 소리가 섞인다. 자체 VAD 가 그걸 발화 종료로 오인하면 인형이
> 아이 말 중간에 끼어든다(R6 미검증). **데모에서는 푸시투토크 버튼이 안전하다.**

**실패**

| `code` | 뜻 | close code | 앱에서 |
|---|---|---|---|
| `UNAUTHORIZED` | `token` 없음·만료·위조 | 1008 | 로그인 화면으로 |
| `TALK_BUSY` | 동시 대화 한도(기본 2) 초과 | 1013 | "잠시 후 다시" — 재시도 가능 |
| `LIVE_UNAVAILABLE` | Live 연결 실패(키 누락·크레딧 소진·네트워크) | 1011 | 대화 종료, `MainHome` 으로 |

에러는 **닫기 전에 `error` 프레임을 먼저 보낸다.** close code 만 보면 원인을 알 수 없으니
프레임의 `code` 로 분기할 것.

**세션이 끊기면 서버가 알아서 다시 붙는다.** 앱의 WS 는 그대로 유지되고 `session_reset`
프레임만 온다. 앱은 이때 "인형이 방금 이야기를 잊었다"고 알면 된다 — 아이가 조금 전에 한
말을 인형이 기억하지 못하므로, 대화를 새로 시작하는 연출이 자연스럽다.

### `GET /healthz`

```json
{ "status": "ok", "api_key": true, "rembg": "loaded",
  "rembg_model": "isnet-general-use", "gemini_model": "gemini-3.1-flash-image",
  "prompt_source": "default:ACTIVE_PROMPT",
  "sprite_count": 5, "sprite_prompt_source": "default:SPRITE_PROMPTS",
  "live_model": "gemini-3.1-flash-live-preview", "doll_voice": "Leda",
  "persona_source": "default:DOLL_PERSONA", "max_talk_sessions": 2 }
```

시연 직전에 한 번 호출해서 모델을 깨워두면 첫 요청이 빨라진다.

**시연 전에 이 두 개를 반드시 볼 것:**

- **`sprite_count` 가 `1` 이면 `SPRITE_SET=0` 으로 떠 있는 것이다.** 등록은 성공하는데
  인형 입이 안 움직인다. 개발용으로 꺼둔 걸 되돌리지 않은 경우다.
- `persona_source` / `sprite_prompt_source` 는 **무엇이 실제로 적용됐는지** 보여준다.
  파일을 고쳤는데 재시작을 안 했거나 JSON 이 깨져 기본값으로 돌아갔으면 여기서 드러난다.

---

## 파이프라인이 하는 일

```
① 로드 + EXIF 회전 보정     0.1초   ← 폰 사진은 회전 태그가 있다
② 리사이즈 (긴 변 1024)     0.2초
③ 사전 누끼 (rembg)         1.2초   ← 손·배경 제거
④ 흰 배경 합성              0.0초
⑤ Gemini 스타일 변환       10.0초
⑥ 코드 누끼 (flood fill)    0.1초   ← 흰 배경을 투명으로
```

**③ 을 빼면 안 된다.** 배경만 보면 없어도 되는 것처럼 보이지만(프롬프트가 흰 배경을 지시하므로),
빼면 **캐릭터가 매번 달라진다** — 같은 프롬프트로 3~4회 돌리면 자세·표정·무늬가 전부 다르게 나온다.
③ 은 배경 제거 수단이 아니라 **일관성 장치**다. 립싱크에서 입 3장이 초당 20~30회 교체될 때
이게 무너지면 캐릭터가 덜덜 떨린다.

**⑥ 이 필요한 이유**: Gemini 는 알파 채널을 만들지 못한다(투명 요구해도 RGB 로 온다).
대신 순백 배경은 완벽하게 만들어주므로(모서리 RGB 254.7±0.6), 그걸 코드로 잘라낸다.

### 스프라이트 5장은 체이닝으로 만든다

```
①~⑥ 으로 만든 base ─┬→ [Gemini] mouth_half  ┐
                     ├→ [Gemini] mouth_open  │ 4장 병렬 (13초)
                     ├→ [Gemini] happy       │
                     └→ [Gemini] sleepy      ┘
```

**원본 사진에서 5장을 각각 뽑지 않는다.** 원본을 매번 새로 해석하게 하면 자세부터
달라진다(③ 을 빼면 안 되는 것과 같은 이유). 이미 정리된 일러스트를 입력으로 주면
해석의 여지가 사라진다. 이 방식으로 **정렬 IoU 99.9% / 몸 색차 4.9 / 변경 면적 4.0%** 를
확인했다(2026-08-13). 비교 기준점: 다른 프롬프트로 뽑은 다른 캐릭터는 IoU 69.2 / 색차 65.2 다.

변형 입력은 **투명 PNG 가 아니라 흰 배경에 다시 얹은 것**이다. 알파를 그대로 넣으면
모델이 검은 배경으로 해석하는 경우가 있다.

생성 결과는 `ai/sprite_test.py --measure` 로 다시 잴 수 있다(무료).

---

## 프롬프트 바꾸기

프롬프트는 AI 파트가 계속 튜닝하는 산출물이라 **코드를 안 고치고도 갈아끼울 수 있다.**

| 우선순위 | 방법 |
|---|---|
| ① | 환경변수 `STYLIZE_PROMPT` — 이미지 재빌드 없이 `.env` 고치고 `docker restart` |
| ② | `server/prompts/stylize.txt` 파일 — 파일만 교체 |
| ③ | 기본값 `ai/doll_stylize_test.py` 의 `ACTIVE_PROMPT` (= 현재 `PROMPT_V3`) |

현재 무엇이 쓰이는지는 `/healthz` 의 `prompt_source` 로 확인한다.

> ⚠️ 프롬프트를 다른 파일에 복사해 두지 말 것. 두 곳에 있으면 검증한 것과 실제로 쓰는 것이
> 갈라지고, 그 사실을 아무도 모른 채 시연에 들어가게 된다.

---

## 환경변수

| 이름 | 기본값 | 설명 |
|---|---|---|
| `GEMINI_API_KEY` | — | **필수** |
| `GEMINI_MODEL` | `gemini-3.1-flash-image` | **Pro 로 바꾸지 말 것** (아래) |
| `GEMINI_ATTEMPTS` | `3` | 빈 응답 재시도 횟수 |
| `REMBG_MODEL` | `isnet-general-use` | 배경 제거 모델 |
| `MAX_EDGE_PX` | `1024` | 리사이즈 기준 |
| `MAX_UPLOAD_BYTES` | `10485760` | 업로드 상한 |
| `PIPELINE_TIMEOUT_SEC` | `180` | 서버 예산 |
| `SPRITE_SET` | `1` | `0` 이면 1장만 만든다(65원). **립싱크가 안 된다** |
| `SPRITE_BACK` | `0` | 뒷모습 추가. **미검증** — 아래 참조 |
| `SPRITE_PARALLEL` | `4` | 변형 동시 생성 수 |
| `SPRITE_PROMPTS` | — | 변형 프롬프트 JSON. 파일: `server/prompts/sprites.json` |
| `MAX_CONCURRENCY` | `2` | 동시 처리 수. 1건당 약 1GB — 메모리 2GB 이하면 `1` |
| `LOCAL_STORAGE_DIR` | `/tmp/doll-sprites` | 결과 PNG 저장 위치. **배포에서는 반드시 바꿀 것** |
| `LIVE_MODEL` | `gemini-3.1-flash-live-preview` | 대화 모델. **Live 지원 모델이어야 한다** |
| `DOLL_VOICE` | `Leda` | 인형 목소리(Google 제공 30개 중 택1) |
| `DOLL_PERSONA` | — | 인형 성격 프롬프트. 파일: `server/prompts/persona.txt` |
| `MAX_TALK_SESSIONS` | `2` | 동시 대화 수. 초과하면 `TALK_BUSY` |
| `LIVE_RECONNECT_ATTEMPTS` | `3` | Live 재연결 시도 횟수 |

**`MAX_TALK_SESSIONS` 와 `MAX_CONCURRENCY` 는 서로 다른 것을 막는다.** 헷갈리면 잘못 튜닝한다.

| | 병목 | 초과하면 |
|---|---|---|
| `MAX_CONCURRENCY` (stylize) | **메모리** — 1건당 약 1GB | 큐에서 대기 |
| `MAX_TALK_SESSIONS` (talk) | **크레딧** — 세션당 18분 상시 스트리밍 | 즉시 거절(`TALK_BUSY`) |

대화를 큐에 세우지 않는 이유: 앞선 대화가 밥 한 끼 동안 안 끝난다. 기다리게 하면 앱은
응답 없는 연결을 붙들고 있게 된다.

> 🔴 **`MAX_TALK_SESSIONS` 를 올리기 전에 R5(Live 비용)를 먼저 재라.** 아직 미측정이고
> 무료 크레딧이 $10 뿐이다. 시연 도중 소진되면 데모가 죽는다.

**🔴 `SPRITE_SET` 은 비용 스위치다.** 목 모드가 없어서 **개발 중 호출도 전부 과금된다.**

| | 호출 | 비용 | 립싱크 |
|---|---|---|---|
| `SPRITE_SET=1` (기본) | 5회 | **325원** | 🟢 |
| `SPRITE_SET=0` | 1회 | 65원 | 🔴 입이 안 움직인다 |

앱 배선을 반복 테스트할 때는 `0` 으로 두고, 5장은 계약 검증과 리허설에서만 돌린다.
무료 크레딧이 $10 뿐이므로 **호출 횟수를 세면서** 할 것.

**`SPRITE_BACK` 은 기본값 `0` 그대로 두는 편이 낫다.** 뒷모습은 R1 지표로 검증할 수 없다 —
나머지 4장은 "base 와 실루엣이 같아야" 통과인데(정렬 IoU 99.9%) 뒷모습은 실루엣이 달라야
정상이라 같은 잣대를 못 댄다. 2D 라 뒷면이 원래 없고, 한 바퀴 도는 동작은 Z축 제자리
스핀으로 대체된다. 켜면 65원이 더 든다.

**`GEMINI_MODEL` 을 Pro 로 바꾸지 말 것.** 비싸고 느린 데다 결과가 나쁘다 — Pro 는 "더 잘
그리려고" 해석을 더 해서 색을 파스텔로 바꾸고 없는 이목구비를 그려 넣는다. 이 과제엔
**덜 해석하는 모델**이 맞다.

**`REMBG_MODEL` 선택 근거** (거북이 인형 실측):

| 모델 | 추론 | 실루엣 IoU | |
|---|---|---|---|
| `isnet-general-use` | **1.2초** | **99.3%** | 🟢 현재 |
| `u2net` | 0.5초 | 78.2% | 🔴 손이 남는다 |
| `birefnet-general-lite` | 15~24초 | 78.0% | 🔴 손이 남는다 |
| `birefnet-general` | 32~49초 | 기준 | 느리다 |

---

## 배포 — 가비아 클라우드 (VM)

**대상 서버**: High CPU / 2vCore / 메모리 4GB / 공인 IP 1개 · 사용 기간 8/18~8/28

Render·Cloud Run 같은 PaaS 가 아니라 **빈 리눅스 한 대**다. 코드를 밀어 넣으면 알아서
돌아가지 않는다. Docker 설치부터 실행 유지·HTTPS 까지 직접 해야 하고, 아래가 그 순서다.

> 🔴 **HTTPS 는 선택이 아니다.** 앱이 `targetSdk 37` 이라 안드로이드가 평문 HTTP 를
> 차단한다. `http://공인IP:8080` 으로는 **앱에서 연결 자체가 안 된다.** ④⑤ 를 건너뛰면
> 서버가 정상이어도 앱은 아무것도 못 한다.

### ① 방화벽 열기 (가비아 콘솔)

`22`(SSH) · `80`(HTTP) · `443`(HTTPS) 세 개. 기본은 22 만 열려 있는 경우가 많다.

**80 이 막혀 있으면 ⑤ 의 인증서 발급이 실패한다.** Let's Encrypt 가 80 번으로 소유권을
확인하기 때문인데, 에러 메시지에 "방화벽"이라는 말이 안 나와서 헤매기 쉽다.

`8080` 은 **열지 않는다.** 서버는 nginx 를 통해서만 노출한다(④ 참조).

### ② Docker 설치

```bash
ssh root@<공인IP>
curl -fsSL https://get.docker.com | sh
```

### ③ 빌드하고 실행

```bash
git clone <저장소> && cd team4_
cp .env.example .env && vi .env      # GEMINI_API_KEY 를 채운다
docker build -t doll-ai .

mkdir -p /data/doll-sprites
docker run -d --name doll-ai --restart always \
  -p 127.0.0.1:8080:8080 \
  -v /data/doll-sprites:/data/doll-sprites \
  -e LOCAL_STORAGE_DIR=/data/doll-sprites \
  -e MAX_CONCURRENCY=2 \
  --env-file .env \
  doll-ai
```

| 옵션 | 이유 |
|---|---|
| `--restart always` | **필수.** 서버가 재부팅되거나 프로세스가 죽어도 되살아난다. PaaS 가 해주던 일을 여기서는 이걸로 대신한다 |
| `-p 127.0.0.1:8080` | **IP 를 반드시 붙인다.** `-p 8080:8080` 으로 쓰면 방화벽과 무관하게 8080 이 인터넷에 그대로 열려서, 평문 HTTP 로 누구나 부를 수 있고 `--proxy-headers`(⑥) 때문에 헤더 위조도 가능해진다 |
| `-v` + `LOCAL_STORAGE_DIR` | **빠뜨리면 안 된다.** 컨테이너를 다시 만드는 순간 등록된 인형 이미지가 전부 사라진다. 기본값 `/tmp` 도 안 된다 — 리눅스가 주기적으로 비운다 |
| `MAX_CONCURRENCY=2` | 요청 1건이 최대 약 **1,036MB**(실측). 4GB 라 2건까지 안전하다 |
| `--env-file .env` | 키를 명령줄에 쓰면 셸 히스토리와 `ps` 에 남는다 |

**오브젝트 스토리지는 쓰지 않는다.** 서버가 결과 PNG 를 디스크에 저장하고 `/files/…` 로
직접 서빙한다. VM 은 디스크가 영속이라 이게 정상 운영 방식이고, 그래서 `-v` 마운트가
빠지면 안 된다.

`Dockerfile` 은 배경 제거 모델을 **빌드 타임에 받아 이미지에 굽는다.** 이 줄을 지우면
컨테이너가 뜰 때마다 모델을 새로 내려받아 첫 요청이 타임아웃난다.

### ④ nginx 리버스 프록시

```bash
apt install -y nginx
```

`/etc/nginx/sites-available/doll` :

```nginx
server {
    listen 80;
    server_name <아래 ⑤ 에서 정한 도메인>;

    client_max_body_size 12M;      # MAX_UPLOAD_BYTES(10MB) + 여유

    # ★ 대화(WebSocket)는 규칙이 완전히 다르다. 반드시 별도 location 으로 둔다.
    location /doll/talk {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;                       # ★ 1.0 으로는 업그레이드가 안 된다
        proxy_set_header Upgrade    $http_upgrade;    # ★ 이 두 줄이 없으면
        proxy_set_header Connection "upgrade";        #    핸드셰이크가 400 으로 실패한다
        proxy_set_header Host              $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;

        proxy_read_timeout  1800s;   # ★ 밥 한 끼(18분) 동안 연결이 유지돼야 한다
        proxy_send_timeout  1800s;
    }

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_read_timeout 240s;       # ★ PIPELINE_TIMEOUT_SEC(180초)보다 커야 한다
        proxy_set_header Host              $host;
        proxy_set_header X-Forwarded-Proto $scheme;   # ★ 아래 ⑥
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    }
}
```

```bash
ln -s /etc/nginx/sites-available/doll /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
```

**`proxy_read_timeout` 이 180초보다 작으면** 오래 걸리는 요청에서 nginx 가 먼저 끊는다.
그러면 앱은 우리가 약속한 `{code, message}` JSON 대신 **nginx 가 만든 HTML 504** 를 받고,
파싱에 실패한다. `client_max_body_size` 기본값(1MB)도 그대로 두면 사진 업로드가
서버에 닿기도 전에 413 으로 잘린다.

#### 🔴 WebSocket 을 별도 location 으로 나눈 이유

**두 엔드포인트가 요구하는 것이 정반대다.** 한 덩어리로 묶으면 둘 중 하나가 깨진다.

| | `/doll/stylize` | `/doll/talk` |
|---|---|---|
| 프로토콜 | HTTP 요청 1건 | **WebSocket 업그레이드** |
| 수명 | 12~35초 | **최대 18분** |
| 데이터 없는 구간 | 없음 | **길다** — 아이가 밥 먹는 동안 아무도 말을 안 한다 |

- `Upgrade`/`Connection` 헤더와 `proxy_http_version 1.1` 이 없으면 **핸드셰이크 자체가
  실패한다.** nginx 기본값은 HTTP/1.0 이고 hop-by-hop 헤더를 전달하지 않는다.
  증상은 앱에서 "연결 안 됨"인데 서버 로그에는 아무것도 안 남아서 원인 찾기가 어렵다.
- `proxy_read_timeout` 을 전체에 1800s 로 주지 않는다. 그러면 매달린 stylize 요청을
  nginx 가 끊어주는 안전망이 사라진다.
- ⑤ 의 certbot 은 이 설정을 읽어 443 블록을 만들어 준다. **certbot 을 돌리기 전에 이
  location 을 넣어 두면** 자동으로 같이 복제된다. 나중에 추가하면 80 과 443 양쪽을
  손으로 고쳐야 하고, 한쪽만 고치면 https 에서만 대화가 안 되는 상태가 된다.

### ⑤ HTTPS — 도메인이 없어도 된다

인증서는 **이름**에만 발급된다. 공인 IP 만으로는 Let's Encrypt 를 못 받는다.
도메인이 따로 없으면 `sslip.io` 를 쓰면 된다 — 등록도 DNS 설정도 필요 없이 IP 를
그대로 이름처럼 쓰게 해주는 공개 서비스다.

```
공인 IP 123.45.67.89  →  123-45-67-89.sslip.io
```

```bash
apt install -y certbot python3-certbot-nginx
certbot --nginx -d 123-45-67-89.sslip.io
```

certbot 이 nginx 설정을 알아서 고치고 443 을 붙여 준다. 이후 앱이 쓸 주소는
`https://123-45-67-89.sslip.io` 다.

> 이 방식이 Let's Encrypt 발급 제한에 걸렸다는 보고가 가끔 있다. 실패하면 `nip.io`
> (`123.45.67.89.nip.io`)로 바꿔 시도할 것. 진짜 도메인이 생기면 그걸 쓰는 게 제일 깔끔하다.

### ⑥ ⚠️ 프록시 뒤에서 이미지 URL 이 http 로 나가는 함정

`routes/stylize.py` 는 `request.base_url` 로 이미지의 절대 URL 을 만든다. uvicorn 은
`X-Forwarded-Proto` 를 읽어 `https` 로 바로잡아 주는데, **그 헤더를 보낸 쪽이 신뢰 목록에
있을 때만** 그렇게 한다. 기본 신뢰 대상은 `127.0.0.1` 뿐이고, **도커 안에서 본 nginx 는
브리지 게이트웨이(`172.17.0.1` 등)** 라 여기 해당하지 않는다.

그래서 그냥 두면 https 로 접속한 앱이 **http 이미지 URL** 을 받고, 안드로이드가 그걸
차단한다. 증상이 **"서버는 200 인데 이미지만 안 보인다"** 라서 원인 찾기가 가장 어렵다.

막는 방법은 두 곳을 맞추는 것이고, 둘 다 이미 반영돼 있다.

- nginx: `proxy_set_header X-Forwarded-Proto $scheme;` (④)
- uvicorn: `--forwarded-allow-ips='*'` (`Dockerfile` CMD)

실측으로 확인한 동작(uvicorn 0.52.1):

| 조건 | `sprites[0]` |
|---|---|
| 헤더 없음 | `http://127.0.0.1:8877/files/…` |
| 헤더 + 신뢰됨 | **`https://123-45-67-89.sslip.io/files/…`** ✅ |
| 헤더 + 신뢰 안 됨 | `http://123-45-67-89.sslip.io/files/…` ← 이게 함정 |

`--proxy-headers` 자체는 uvicorn 기본값이 이미 켜짐이라, **실제로 결정하는 것은
`--forwarded-allow-ips` 다.** 그리고 `'*'` 는 **③ 에서 8080 을 `127.0.0.1` 에만
바인딩했다는 전제**로만 안전하다. 외부에 직접 열면 아무나 헤더를 위조할 수 있다.

### ⑦ 확인

```bash
curl https://<도메인>/healthz
# {"status":"ok","api_key":true,"rembg":"loaded",...}
```

`api_key` 가 `false` 면 `.env` 가 컨테이너에 안 들어간 것이다. `rembg` 가 `loaded` 가
아니면 워밍업이 실패한 것이고, 첫 요청이 그만큼 느려진다.

실제 변환까지 확인 — 🔴 `/doll/stylize` 도 이제 로그인이 필요하다:

```bash
TOKEN=$(curl -s -X POST https://<도메인>/api/auth/login -H "Content-Type: application/json" \
  -d '{"account":"...","password":"..."}' | python -c "import json,sys;print(json.load(sys.stdin)['token'])")

curl -F image=@doll.jpg https://<도메인>/doll/stylize -H "Authorization: Bearer $TOKEN"
# sprites[0] 가 https:// 로 시작하는지 반드시 확인할 것 (⑥)
```

**대화(WS)는 curl 로 확인할 수 없다.** `ai/talk_client.py` 를 쓴다 — 앱이 하는 일을
그대로 흉내내는 클라이언트다. 여기도 `--token`(위에서 받은 것과 동일)이 필수다.

```bash
# ① 배선만 (오디오를 안 보내므로 사실상 무료)
.venv/bin/python -m ai.talk_client --protocol-only --url wss://<도메인>/doll/talk --token $TOKEN

# ② 실제 왕복 — 인형이 대답하고 TTFB 가 나온다
.venv/bin/python -m ai.talk_client ai/out/dialog/child_draw.wav --url wss://<도메인>/doll/talk --token $TOKEN

# ③ 18분 소크 — Live 비용(R5)과 세션 한계를 동시에 잰다. 크레딧을 많이 쓴다
.venv/bin/python -m ai.talk_client ai/out/dialog/child_draw.wav --soak 18m --url wss://... --token $TOKEN
```

`wss://` 다. 앱이 `targetSdk 37` 이라 평문은 어차피 차단된다.

**로컬 실측 (2026-08-15)**

```
턴  1  TTFB 0.77초  응답 4.4초  →  「와 그림 그렸어? 뭐 그렸어? 나도 보여줘!」
Live 세션 종료 — usage={'turns': 1, 'total_sum': 750, 'by_modality': {'text': 300, 'audio': 423}} dropped=0
```

- **TTFB 는 1.2초 이하여야 한다** (순수 Live 실측 0.85초 + 서버 중계). 넘으면 아이가
  "인형이 죽었다"고 느낀다.
- **`dropped` 가 0 이 아니면 오디오가 버려지고 있다.** 특히 대화가 아예 무응답인데
  `dropped` 가 몇 개 찍혀 있다면, 버려진 것이 `activity_start` 일 가능성이 높다.
- ①이 통과하는데 ②가 35초 무응답이면 **nginx 는 정상이고 activity 신호 쪽 문제**다.
  ①이 실패하면 nginx 의 WebSocket 설정(④)을 먼저 본다.

### 갱신

```bash
git pull && docker build -t doll-ai . && docker restart doll-ai
```

컨테이너를 지우고 새로 만들어도 `-v` 로 마운트한 `/data/doll-sprites` 는 남는다.

### 운영 메모

- **콜드스타트가 없다.** 항상 켜져 있으므로 시연 전 워밍 핑이 필요 없다.
- **로그**: `docker logs -f doll-ai`
- **기간 제약**: 서버 지원이 **8/28 까지**다. 제출 요건이 "행사 종료까지 작동 유지"이므로,
  그 뒤로도 살려둬야 한다면 다른 곳으로 옮겨야 한다. 같은 `Dockerfile` 이 그대로 돈다.
  ⚠️ 옮길 곳은 **영구 디스크를 붙일 수 있어야 한다.** 결과 PNG 를 파일시스템에 두므로,
  파일시스템이 휘발성인 플랫폼에 그냥 올리면 인스턴스가 바뀔 때 등록된 인형이 사라진다.

---

## 알려진 한계

- **흰색·크림색 인형**은 실패할 수 있다. ⑥ 이 "흰색 = 배경"으로 판정하므로 흰 인형은
  몸통이 잘려나간다. 시연용 인형은 유색으로 고를 것.
- 프롬프트는 **거북이 인형 하나로만** 튜닝했다. 곰인형처럼 팔다리가 뚜렷한 인형에서는
  다르게 동작할 수 있다.
- 스프라이트 5장은 **거북이 인형 하나로만 검증**했다(정렬 IoU 99.9%). 다른 인형에서도
  base 와 실루엣이 유지되는지는 미확인 — 립싱크가 흔들리면 여기를 먼저 의심할 것.
- 뒷모습(`SPRITE_BACK`)은 **미검증**이라 기본값이 꺼짐이다. 한 바퀴 도는 동작은 Z축
  제자리 스핀으로 대체한다.
