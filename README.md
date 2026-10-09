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
2. 오디오 파일을 올리거나 전사 텍스트를 붙여넣고 **제출**
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
    templates/meetalign/
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
