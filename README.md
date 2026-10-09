# MeetAlign (심화프로젝트랩 4조 프로토타입)

회의가 끝난 뒤 "다들 같은 걸 이해했다"고 믿는 **합의 착시**를 줄이는 웹 서비스입니다.

진행자가 회의 녹음을 올리면 → 통합 회의록이 나오고 → 이해가 엇갈릴 만한 확인 문제가 만들어지고 →
참가자가 풀면 진행자가 "몇 명이 다르게 이해했는지" 집계를 보고 → 참가자가 익명으로 질문합니다.

## 실행 방법

Windows(Git Bash) 기준입니다.

```bash
# 1. 가상환경
python -m venv .venv
source .venv/Scripts/activate

# 2. 패키지 설치
pip install -r requirements.txt

# 3. 설정 파일 (키가 필요 없는 mock 공급자가 기본값)
cp .env.example .env

# 4. DB 준비와 데모 데이터
cd APL_04
python manage.py migrate
python manage.py seed_demo

# 5. 개발 서버
python manage.py runserver
```

브라우저에서 <http://127.0.0.1:8000/meetalign/> 로 접속합니다.

### 데모 계정

`seed_demo` 가 단일 팀 `4조` 와 계정 3개를 만듭니다. 비밀번호는 모두 `demo1234!` 입니다.

| 아이디 | 역할 |
| --- | --- |
| `host` | 진행자 |
| `member1` | 참가자 |
| `member2` | 참가자 |

### 시연 순서

1. `host` 로 로그인 → `4조` 회의 목록 → `스터디앱 1차 배포 준비` 회의 입장
2. **오디오 파일** 탭에서 파일을 올리거나, **텍스트 붙여넣기** 탭에
   `samples/demo_meeting.txt` 내용을 붙여넣고 **회의록과 확인 문제 만들기**
   - mock STT 는 파일 내용을 읽지 않고 가상 회의 전사를 돌려줍니다. 어떤 파일이든 됩니다.
   - 붙여넣기만으로도 끝까지 시연됩니다.
3. 통합 회의록 확인 → `.md` 다운로드
4. **확인 문제 검토·승인** 에서 문항·보기·정답·근거를 고치고 **승인하고 참가자에게 공개**
5. 다른 브라우저(또는 시크릿 창)에서 `member1`, `member2` 로 로그인 → **확인 문제 풀기** → 자동 채점
6. 다시 `host` 로 **이해 엇갈림 집계** 확인 — 문항별 인원수만 보이고 개인 이름은 나오지 않습니다.

## 환경변수

`.env.example` 을 `.env` 로 복사해서 씁니다. **`.env` 는 커밋하지 않습니다.**

| 변수 | 설명 |
| --- | --- |
| `DJANGO_SECRET_KEY` | 비우면 로컬 시연용 기본값을 씁니다. |
| `DJANGO_DEBUG` | `1` 이면 디버그 모드. |
| `DATABASE_URL` | 비우면 SQLite(`APL_04/db.sqlite3`). 값이 있으면 PostgreSQL 설정으로 씁니다. |
| `STT_PROVIDER` | `mock` \| `gemini` |
| `LLM_PROVIDER` | `mock` \| `gemini` |
| `GEMINI_API_KEY` | Phase 2 에서 사용. |
| `GEMINI_STT_MODEL`, `GEMINI_LLM_MODEL` | 모델 ID 는 코드에 박지 않고 환경변수로 받습니다. |

## 테스트

```bash
cd APL_04
python manage.py test
```

## 화면 스타일 (CSS 빌드)

빌드된 CSS(`APL_04/static/css/tailwind.css`)를 **커밋해 두었기 때문에, 화면만 볼 때는 아무것도 설치하지 않아도 됩니다.**
템플릿의 클래스를 고쳤을 때만 아래 명령으로 다시 빌드합니다.

```bash
cd APL_04
PYTHONUTF8=1 python manage.py tailwind build     # 한 번 빌드
PYTHONUTF8=1 python manage.py tailwind watch     # 고치는 동안 자동 빌드
```

- **Windows 에서는 `PYTHONUTF8=1` 이 필요합니다.** 없으면 CLI 가 출력하는 이모지 때문에
  `UnicodeEncodeError: 'cp949' codec` 이 납니다 (빌드 자체는 되지만 로그가 깨집니다).
- Node 와 npm 은 쓰지 않습니다. `django-tailwind-cli` 가 Tailwind 공식 standalone 실행 파일을
  `APL_04/.django_tailwind_cli/` 에 내려받아 씁니다(첫 빌드 때 자동, `.gitignore` 처리됨).
- Basecoat 의 CSS/JS 는 `APL_04/static/vendor/` 에 내려받아 로컬로 서빙합니다.
  **런타임에 외부 CDN 을 부르지 않으므로 네트워크가 끊겨도 화면이 그대로 나옵니다.**

## UI 가이드

### 쓰는 도구

| 구분 | 무엇 | 어디서 고치나 |
| --- | --- | --- |
| 유틸리티 클래스 | Tailwind CSS 4 (`flex`, `mt-4`, `text-sm` …) | 템플릿에서 바로 |
| 컴포넌트 클래스 | Basecoat 1.0.2 (`btn`, `card`, `badge`, `field`, `input`, `tabs`, `breadcrumb`, `alert`, `progress`) | 템플릿에서 바로 |
| 디자인 토큰 | `APL_04/tailwind_src/input.css` 의 `:root` | 색·반경을 바꿀 때 여기 한 곳만 |

`base.html` 에서 **Tailwind 를 먼저, Basecoat 을 나중에** 읽습니다.
이 순서로 CSS 레이어가 잡혀서, 컴포넌트 기본 모양을 유틸리티 클래스로 덮어쓸 수 있습니다
(예: 36px 인 `.btn` 에 `min-h-11` 을 더해 44px 터치 영역을 만듭니다).

### 규칙

- 흰 배경 + 얇은 회색 테두리(`--border: #e5e7eb`), 그림자는 거의 쓰지 않습니다. 반경 8px.
- 본문 `#1f2937`, 보조 설명 `#6b7280`, 강조는 파랑 `#2563eb` 하나만 씁니다.
- 빨강은 삭제·오류에만, 초록은 완료·정답에, 앰버는 주의와 **"다르게 이해함"** 에 씁니다.
  참가자에게 "틀렸다"로 읽히지 않도록 빨강을 쓰지 않습니다.
- 본문 영역은 가운데 정렬 800px 단일 컬럼(`max-w-[800px]`), 모바일에서 좌우 여백 16px.
- 누르는 것은 최소 44px(`min-h-11`), 모든 입력에는 `<label>` 을 붙입니다.
- 사람 이름은 집계 화면에 절대 쓰지 않습니다(문항별 인원수만).

### 공용 템플릿 조각

| 파일 | 용도 |
| --- | --- |
| `base.html` | 헤더, breadcrumb 블록, 본문 컨테이너, 접이식 "이 화면 안내" |
| `_meeting_crumbs.html` | 회의 안 화면의 breadcrumb (`here` 로 현재 화면 이름을 넘김) |
| `_meeting_tabs.html` | 회의 안 화면 이동 세그먼트 (`current` 로 현재 화면을 넘김) |
| `_minutes_sections.html` | 저장된 회의록 마크다운을 섹션 카드로 그림 |
| `templatetags/ui_extras.py` | 화면 표현만 돕는 필터 (`md_sections`, `top_wrong_index`, `percent`) |

JavaScript 는 세 군데만 씁니다: 제출 버튼 로딩 표시, 입력 방식 탭(Basecoat), 문제 풀이의 선택 개수 표시.
접었다 펴는 것은 모두 `<details>` 로 처리해 JS 가 없어도 동작합니다.

## 구조

```
APL_04/
  manage.py
  setting/            # 설정 패키지
  meetalign/
    models.py         # Team, Meeting, QuizItem, Response, AnonQuestion
    views.py
    urls.py
    services/
      stt.py          # 음성 전사 (공급자: mock | gemini)
      llm.py          # 회의록/확인 문제 생성 (공급자: mock | gemini)
    management/commands/seed_demo.py
    templatetags/ui_extras.py   # 화면 표현용 필터
    templates/meetalign/
  tailwind_src/input.css        # 디자인 토큰 (색, 반경, 폰트)
  static/
    css/tailwind.css            # 빌드 결과물 (커밋함)
    vendor/                     # Basecoat CSS/JS 로컬 사본
samples/demo_meeting.txt        # 붙여넣기 시연용 전사 텍스트
```

## 범위 밖

회원가입, 팀 생성과 초대 코드, 비동기 처리(Celery/스레드), 오디오 분할, 재시도 로직,
배포, Supabase 실제 연결, 화자 분리. 목업에 이미 있던 화면(`signup`, `team_join`)은
화면만 남겨 두었습니다.

---

## 참고: 최초 화면 구성 문서

```
3.화면 구성
3.1.index: 회원가입/로그인/로그아웃 버튼, 팀 생성/참여 링크
3.2.1 회원가입/로그인/로그아웃(자체적인 로그인만 구현)
3.2.2 팀 생성/참여 화면
3.3. 회의방:
3.3.1.1 소속된 팀의 회의 목록: 팀의 회의 중 하나를 선택해서 입장
3.3.1.2 회의 생성 화면
3.3.2. 회의 상세 페이지: 회의 녹음 입력, 제출 버튼, 종료버튼(이후엔 3.5)
3.3.3. 제출 이후 페이지: ai가 통일 회의록을 만드는 로딩 바->다운로드 버튼
3.3.4 대화창(프로젝트별로 회의마다의 대화방 생성한뒤, 프로젝트의 맥락은 각 회의의 결과로만 맥락유지)
3.3.4.1 llm 채팅화면: 익명 질문/llm 대화 토글
3.3.4.2 익명 질문 받는 쪽 화면
3.4 회의 llm요약
3.5 생성된 질문에 대한 답변
3.6 개별 이해도 검증화면
```
