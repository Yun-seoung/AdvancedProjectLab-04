# 퀴즈 기능 완전 삭제 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** MeetAlign에서 이해도 확인 퀴즈(CheckQuestion·Answer·`Meeting.check_questions_transcript` 및 그 뷰·URL·템플릿·집계 모듈·전용 services 함수·전용 테스트)를 모델까지 전부 삭제하고, 남은 기능(익명 질문함·회의 요약·결정 컴파일러)과 문서·주석을 일관된 상태로 되돌린다.

**Architecture:** 순수 삭제 작업이다. 퀴즈 진입점은 PR #40에서 이미 모든 템플릿에서 제거되어 URL만 고아로 남아 있으므로 내비게이션 변경이 없다. 팀 홈 배지는 이미 `decision_status`로 전환되어 있어 `quiz_status.py`를 통째로 지워도 영향이 없다. `services.py`는 퀴즈 전용 4개 심볼만 지우고, 결정 컴파일러가 직접 호출하는 `_generate_clean`·`_normalize_for_match`·`_enabled`·`_is_korean_or_english`는 그대로 둔다. 마이그레이션은 `makemigrations`가 만든 `0021` 하나로 끝낸다.

**Tech Stack:** Django 5 / Python 3.14 / SQLite / `python manage.py test` (unittest)

**Spec:** 이 문서의 「0. 승인된 범위 판정」절이 사양을 겸한다 (2026-11-04 사전 조사 보고 + 사용자 승인).

---

## 0. 승인된 범위 판정

### 0.1 베이스라인

```
branch  chore/remove-quiz   (clean, HEAD = 1a3bcdd)
tests   Ran 372 tests ... OK  (약 141초)
URL     22개 (urls.py name= 22)
```

### 0.2 삭제 — 퀴즈 전용

| 대상 | 위치 |
|---|---|
| 모델 `CheckQuestion` | `APL_04/meetalign/models.py:121-144` |
| 모델 `Answer` | `APL_04/meetalign/models.py:147-164` |
| 필드 `Meeting.check_questions_transcript` | `APL_04/meetalign/models.py:43-45` (주석 2줄 포함) |
| 모듈 전체 | `APL_04/meetalign/quiz_status.py` (130줄) |
| 뷰 `_find_discrepancies` `_create_check_questions` `question_review` `understanding` `verification` | `APL_04/meetalign/views.py:285-421` |
| import | `views.py:15` (`quiz_status`), `views.py:17` (`Answer`, `CheckQuestion`) |
| URL 3개 `understanding` `question_review` `verification` | `APL_04/meetalign/urls.py:21-23` |
| 템플릿 3개 | `templates/meetalign/understanding.html` `question_review.html` `verification.html` |
| services 심볼 4개 `FAKE_CHECK_QUESTIONS` `_parse_mcq_json` `generate_check_questions` `generate_check_questions_from_text` | `services.py:28-30, 398-432, 434-441, 443-469` |
| 테스트 52개 | Task 3 표 참조 |

### 0.3 삭제 금지 — 퀴즈처럼 보이지만 아닌 것

- **`answers` 뷰·URL·`answers.html`** — `Question.answer`(익명 질문함)의 주최자 답변 화면. 퀴즈 아님.
- **`inbox` `chat` `summary` `meeting_result` `Question` 모델** — 퀴즈 모델 의존 0.
- **`views.py:94 _meeting_rows` + `meeting_list.html` 배지** — 이미 `decision_status` 기준.
- **`base.html` `.pending-badge` / `.pending-clear` / `.item-actions` CSS** — 결정 확인 배지가 현재 사용 중. **주석만** 수정.
- **`services._generate_clean` `_normalize_for_match` `_enabled` `_is_korean_or_english` `_FOREIGN_SCRIPT`** — `decision_compiler.py:19,22,25,256,273,296,305,348`에서 직접 호출.
- **`admin.py` `tasks.py` `decision_compiler.py` `decision_status.py` `context_processors.py` `apps.py`** — 코드 참조 0건. `decision_compiler.py`는 주석 2곳만 수정.

### 0.4 계획 작성 중 발견한 추가 사항 (승인 확인 필요)

> 아래 3건은 승인 지시에 명시되지 않았지만 지시를 그대로 수행하면 반드시 마주치는 것들이다. 계획에는 **권장안대로 반영**해 두었으니, 다르게 가려면 승인 시 알려주면 된다.

**(가) 커밋 순서를 ③테스트 → ①코드 → ②모델 로 바꿀 것을 권장한다.**
커밋 5개와 각 커밋의 내용은 지시대로 유지한다. 순서만 바꾸는 이유: `tests.py:18`이 모듈 최상단에서 `quiz_status`를, `:20`에서 `Answer`·`CheckQuestion`을 import하므로, 코드를 먼저 지우면 **테스트 수집 자체가 ImportError로 죽어** 커밋 ①②에서 테스트를 한 줄도 돌릴 수 없다. 테스트를 먼저 정리하면 **다섯 커밋 전부 `Ran 320 tests ... OK`** 로 green·bisect 가능해진다. 지시 순서를 유지하려면 ①②의 검증 게이트가 `manage.py check` + `sqlmigrate`로만 제한된다.

**(나) 문서 수정 지점이 지시에 적힌 7곳보다 많다.**
지시의 줄 번호는 `퀴즈|CheckQuestion` 그렙 결과에서 나온 것이고, `이해도|불일치|문제|understanding|verification`까지 넓히면 실제 지점은 **38곳**이다. 지시된 7곳만 고치면 "확인 퀴즈 미완료 인원 배지"는 사라지는데 바로 위 `20·21행`의 "4. 이해도 확인 문제 / 5. 불일치 탐지"는 남아 문서가 더 어긋난다. Task 4에 **38곳 전체**를 열거했고, 지시에 명시된 7곳은 표에 `★`로 표시했다.

**(다) 두 사양서는 퀴즈와 무관하게 이미 낡아 있다 — 이번 범위에서 제외한다.**
`자동 테스트 181개`(실제 372), `모델 8개`·`URL 19개`(실제 10개·22개), URL 표·모델 표에 결정 컴파일러(PR #38) 항목 없음. 이걸 전부 현행화하는 것은 "퀴즈 삭제"보다 큰 작업이므로 **하지 않는다.** Task 4는 (1) 퀴즈 서술 제거·전환, (2) 내가 건드린 줄은 사실과 맞게, (3) URL 표에서 퀴즈 3행을 빼고 결정 컴파일러 3행을 넣어 `URL 19개` 문구를 참으로 복구, (4) 모델 표의 낡음은 진행사항.txt 알려진 이슈로 기록, 까지만 한다.

---

## Global Constraints

- 새 CSS 규칙·새 CDN·새 패키지 추가 금지. `base.html`의 기존 CSS는 주석만 수정.
- `.env`·API 키·`db.sqlite3`·`media/` 커밋 금지.
- 새 마이그레이션은 `0021` **하나만**. 손으로 쓰지 않고 `makemigrations`로 생성.
- `git add`는 **항상 파일 경로를 명시**. `git add .` / `git add -A` 금지.
- `git push` 금지 — 사용자가 직접 한다.
- `services.py`는 0.2의 4개 심볼 삭제만. 다른 함수는 한 줄도 손대지 않는다.
- 커밋 메시지는 기존 한국어 관행을 따르고, 끝에 `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`를 붙인다.
- 작업 디렉터리: 모든 `manage.py` 명령은 `C:\dev\AdvancedProjectLab-04\APL_04`에서 실행.

## Review Focus

퀴즈가 사라진 뒤 아무 태스크의 테스트도 짚지 않는데 실제로 문제가 될 수 있는 지점들. 각 줄의 테스트를 담당 태스크에 넣어 두었다.

1. **옛 퀴즈 URL 북마크** — `/meetings/<id>/understanding/` 등에 들어오면 500이 아니라 404여야 한다. → Task 1 Step 4.
2. **데이터가 들어 있는 DB에 0021 적용** — 퀴즈 행이 있는 상태에서 테이블이 사라져야 하고, 다른 테이블은 건드리지 않아야 한다. → Task 2 Step 3(`sqlmigrate` 확인) + Step 6(`migrate` 왕복).
3. **`Meeting.save(update_fields=[...])` FieldError** — 삭제된 필드가 `update_fields`에 남으면 터진다. 조사 결과 `tasks.py:41`은 `["transcript","record","processing_status"]`로 안전함을 확인했고, Task 2에서 회귀로 고정한다. → Task 2 Step 1.
4. **`_generate_clean` 재시도 커버리지 소실** — 언어 필터 재시도 1회 로직은 결정 컴파일러가 쓰는 공용 경로인데, 현재 그 테스트 2개가 `generate_check_questions`를 매개로만 검증한다. 퀴즈와 함께 지우면 커버리지가 조용히 사라진다. → Task 3 Step 1에서 `summarize` 기반으로 이전.
5. **익명 질문함 회귀** — `answers`/`inbox`는 이름이 비슷해 같이 지워지기 쉽다. 삭제 후에도 질문 저장·본인만 열람이 살아 있어야 한다. → Task 1 Step 4.

---

### Task 3-first 실행 순서

```
Task 1 (commit ③ 테스트 정리)   → Ran 320 tests ... OK
Task 2 (commit ① 코드·템플릿·URL) → Ran 320 tests ... OK
Task 3 (commit ② 모델·0021)      → Ran 320 tests ... OK
Task 4 (commit ④ 문서·주석)       → Ran 320 tests ... OK
Task 5 (commit ⑤ 진행사항)        → 최종 전수 검증
```

> 아래 태스크 번호는 **실행 순서**, 괄호 안 동그라미 번호는 **지시된 커밋 분류**다.

---

### Task 1 — 테스트 정리 (commit ③)

**Files:**
- Modify: `APL_04/meetalign/tests.py`

**Interfaces:**
- Consumes: 없음 (첫 태스크)
- Produces: 이후 모든 태스크의 검증 기준인 `Ran 320 tests ... OK`. `tests.py`에 `quiz_status`·`Answer`·`CheckQuestion`·`check_questions*`·`understanding`·`question_review`·`verification` 참조가 0건이 된다.

#### 1-A. 클래스 통째로 삭제 — 26개

| 클래스 | 줄 | 테스트 수 |
|---|---|---|
| `DiscrepancyTests` | 1173-1221 | 5 |
| `QuestionTimingTests` | 1223-1316 | 7 |
| `QuizPendingTests` | 1788-1923 | 10 |
| `PartialAnswerNoticeTests` | 1925-1985 | 4 |

클래스 본문의 `setUp`·`_question`·`_solve`·`_home`·`_review`·`_quiz_pending`·`_answer` 헬퍼까지 함께 사라진다.

#### 1-B. 메서드 단위 삭제 — 26개

| 클래스 | 수 | 메서드 |
|---|---|---|
| `PrototypeFlowTests` | 11 | `test_meeting_screen_has_no_quiz_entry`, `test_question_review_creates_draft_questions_once`, `test_questions_hidden_from_participants_until_published`, `test_publish_makes_questions_visible_to_participants`, `test_host_can_edit_question_before_publishing`, `test_host_can_edit_all_questions_in_one_submit`, `test_understanding_saves_and_prefills_answer`, `test_understanding_resubmit_updates_not_duplicates`, `test_same_question_can_hold_different_users_answers`, `test_discrepancy_flagged_when_someone_picks_wrong_answer`, `test_no_discrepancy_when_everyone_picks_correct_answer` (+ `_published_question` 헬퍼) |
| `LocalLLMTests` | 9 | `test_generate_check_questions_parses_llm_json`, `test_generate_check_questions_caps_at_five`, `test_no_transcript_uses_fake_even_when_enabled`, `test_malformed_json_falls_back_to_fake`, `test_parse_mcq_json_drops_malformed_items_but_keeps_valid_ones`, `test_parse_mcq_json_drops_items_with_duplicate_choices`, `test_parse_mcq_json_extracts_array_even_with_surrounding_prose`, `test_parse_mcq_json_drops_items_with_hallucinated_evidence`, `test_generate_check_questions_uses_transcript_not_record_as_source` (+ `_mcq_json` 헬퍼) |
| `LanguageFilterTests` | 1 | `test_check_questions_rejects_mixed_language_output` |
| `DesignAlignmentTests` | 1 | `test_verification_page_is_separate_from_answer_page` |
| `FollowupTests` | 3 | `test_duplicate_check_question_order_is_rejected_by_db`, `test_question_review_does_not_duplicate_existing_questions`, `test_verification_lists_members_who_have_not_answered` (+ `_published_question` 헬퍼) |
| `DecisionModelTests` | 1 | `test_existing_quiz_tables_are_untouched_by_the_new_feature` |

**삭제 합계 = 26 + 26 = 52.  372 − 52 = 320.**

#### 1-C. 수정해서 유지 — 7개 (퀴즈를 도구로만 쓰는 퀴즈 무관 테스트)

- [ ] **Step 1: 7개 테스트를 퀴즈에서 떼어낸다**

| 테스트 | 수정 |
|---|---|
| `LanguageFilterTests.test_retries_once_then_succeeds_on_clean_output` | `services._generate`가 `["这是中文", "정상 요약문"]`을 내도록 하고 `services.summarize("원문")`을 호출. `결과 == "정상 요약문"`, `gen.call_count == 2` 어서션 유지 |
| `LanguageFilterTests.test_gives_up_after_one_retry_still_garbled` | `_generate`가 계속 `"这是中文"`이면 `services.summarize("원문") == "원문"`(폴백), `gen.call_count == 2` |
| `LocalLLMTests.test_disabled_without_model_env_keeps_fake_behavior` | `tests.py:439` 퀴즈 어서션 1줄 삭제. `chat_reply`의 `"(가짜 LLM 응답)"` 어서션 유지 |
| `LocalLLMTests.test_llm_failure_falls_back` | `tests.py:485` 1줄 삭제. `"LLM 연결 실패"` 어서션 유지 |
| `test_gemini_http_error_falls_back` | `tests.py:1448` 1줄 삭제. `"LLM 연결 실패"` 어서션 유지 |
| `PrototypeFlowTests.test_meeting_create_and_all_screens` | `tests.py:124-125` 화면 목록에서 `"understanding"` 제거 |
| `PrototypeFlowTests.test_sub_screens_have_back_to_meeting_link` | `tests.py:217` 목록에서 `"understanding"` 제거 |

위 두 `LanguageFilterTests` 수정이 Review Focus 4를 덮는다 — `_generate_clean`의 재시도 1회/포기 경로가 `summarize` 경유로 계속 검증된다.

- [ ] **Step 2: import와 남은 주석 정리**

`tests.py:18`에서 `quiz_status` 제거 → `from . import decision_compiler, decision_status, services, tasks, views, whisper_worker`
`tests.py:20`에서 `Answer`, `CheckQuestion` 제거.
`tests.py:2527` docstring의 "기존 CheckQuestion/Answer 테이블과 완전히 분리된 새 테이블이라는 것" 문구를 삭제된 모델을 가리키지 않게 고친다.
`test_an_agreement_can_be_changed_like_an_existing_quiz_answer`는 **내용이 `DecisionAgreement`라 유지**하고, 이름만 `test_an_agreement_can_be_changed_after_it_is_saved`로 바꾼다.

- [ ] **Step 3: 테스트 실행 — 320개 green 확인**

Run: `cd C:\dev\AdvancedProjectLab-04\APL_04 && python manage.py test`
Expected: `Ran 320 tests` + `OK`.
320이 아니면 멈추고 실제 수와 1-A/1-B 표의 차이를 보고한다(계획을 고치는 쪽이지 테스트를 맞추는 쪽이 아니다).

- [ ] **Step 4: 퀴즈 참조가 남지 않았는지 확인**

Run: `grep -n "quiz_status\|CheckQuestion\|check_question\|Answer\b" APL_04/meetalign/tests.py`
Expected: 출력 없음.

- [ ] **Step 5: 커밋 ③**

```bash
git add APL_04/meetalign/tests.py
git commit -m "테스트: 퀴즈 전용 테스트 52개 삭제, LLM 인프라 테스트는 요약 경유로 이전"
```

---

### Task 2 — 퀴즈 코드·템플릿·URL 삭제 (commit ①)

**Files:**
- Delete: `APL_04/meetalign/quiz_status.py`
- Delete: `APL_04/meetalign/templates/meetalign/understanding.html`
- Delete: `APL_04/meetalign/templates/meetalign/question_review.html`
- Delete: `APL_04/meetalign/templates/meetalign/verification.html`
- Modify: `APL_04/meetalign/views.py` (15, 17, 285-421행)
- Modify: `APL_04/meetalign/urls.py` (21-23행)
- Modify: `APL_04/meetalign/services.py` (28-30, 398-469행)

**Interfaces:**
- Consumes: Task 1의 green 320개.
- Produces: `urls.py`의 name 19개. `services`가 외부에 내보내는 함수는 `transcribe` `stt_status_text` `clova_used_seconds` `summarize` `chat_reply` + 결정 컴파일러용 내부 함수들만 남는다.

- [ ] **Step 1: 파일 4개 삭제**

```bash
git rm APL_04/meetalign/quiz_status.py \
       APL_04/meetalign/templates/meetalign/understanding.html \
       APL_04/meetalign/templates/meetalign/question_review.html \
       APL_04/meetalign/templates/meetalign/verification.html
```

- [ ] **Step 2: `views.py` 정리**

`:15` → `from . import decision_compiler, decision_status, services, tasks`
`:17` 모델 import에서 `Answer`, `CheckQuestion` 제거.
`:285-421` 다섯 함수(`_find_discrepancies`, `_create_check_questions`, `question_review`, `understanding`, `verification`)를 삭제한다. `answers`(274-282)와 `record_file`(424-431) 사이가 비는 형태가 된다.
`:434-437` 결정 컴파일러 구획 주석의 "기존 이해도 퀴즈 화면(question_review/understanding/verification)은 그대로 두고, 그 옆에 별도 흐름으로 붙인다"를 삭제된 화면을 가리키지 않게 고친다.

- [ ] **Step 3: `urls.py`에서 퀴즈 URL 3개 삭제**

`understanding` / `question_review` / `verification` `path(...)` 3줄 제거. 나머지 19개는 순서·이름 그대로.

- [ ] **Step 4: `services.py`에서 퀴즈 전용 4개 심볼만 삭제**

`FAKE_CHECK_QUESTIONS`(28-30), `_parse_mcq_json`(398-432), `generate_check_questions`(434-441), `generate_check_questions_from_text`(443-469).
`_generate_clean`(382) · `_normalize_for_match`(394) · `_enabled`(49) · `_is_korean_or_english`(53) · `_FOREIGN_SCRIPT`(42)는 **그대로 둔다.** `json`·`re` import는 `summarize`/`chat_reply`/언어 필터가 계속 쓰는지 확인한 뒤에만 건드린다(기본: 건드리지 않음).

- [ ] **Step 5: 회귀 테스트 3개 추가** (Review Focus 1·5)

`PrototypeFlowTests`에 넣는다.

```python
def test_old_quiz_urls_are_gone(self):
    for path in ("understanding/", "questions/review/", "verification/"):
        r = self.client.get("/meetings/%d/%s" % (self.meeting.id, path))
        self.assertEqual(r.status_code, 404, path)

def test_quiz_url_names_no_longer_resolve(self):
    for name in ("understanding", "question_review", "verification"):
        with self.assertRaises(NoReverseMatch, msg=name):
            reverse(name, args=[self.meeting.id])

def test_anonymous_question_flow_survives_quiz_removal(self):
    self.client.post(reverse("chat", args=[self.meeting.id]), {"mode": "anon", "text": "질문입니다"})
    q = Question.objects.get(meeting=self.meeting)
    self.client.post(reverse("answers", args=[self.meeting.id]), {"question_id": q.id, "answer": "답변입니다"})
    q.refresh_from_db()
    self.assertEqual(q.answer, "답변입니다")
    self.assertContains(self.client.get(reverse("inbox", args=[self.meeting.id])), "답변입니다")
```

`tests.py` 상단 import에 `NoReverseMatch`를 추가한다: `from django.urls import NoReverseMatch, reverse`.
**이 3개가 들어가므로 이후 기준 수는 320 → 323이다.**

- [ ] **Step 6: 검증**

Run: `python manage.py check`
Expected: `System check identified no issues`.

Run: `python manage.py test`
Expected: `Ran 323 tests` + `OK`.

- [ ] **Step 7: 커밋 ①**

```bash
git add APL_04/meetalign/views.py APL_04/meetalign/urls.py APL_04/meetalign/services.py APL_04/meetalign/tests.py
git commit -m "퀴즈 삭제: 뷰·URL·템플릿·quiz_status·퀴즈 전용 services 함수 제거"
```
(`git rm`한 4개 파일은 이미 index에 올라가 있다.)

---

### Task 3 — 모델 + 마이그레이션 0021 (commit ②)

**Files:**
- Modify: `APL_04/meetalign/models.py` (43-45, 121-167행)
- Create: `APL_04/meetalign/migrations/0021_*.py` (**`makemigrations`가 생성** — 손으로 쓰지 않는다)
- Modify: `APL_04/meetalign/tests.py` (회귀 테스트 2개 추가)

**Interfaces:**
- Consumes: Task 2 종료 시점의 green 323개.
- Produces: `meetalign` 앱의 모델 10개(`Team` `Meeting` `Recording` `SttUsage` `ChatMessage` `Question` `Decision` `Interpretation` `DecisionAgreement` `Checkpoint`). `Meeting`에 `check_questions_transcript` 없음, `User`에 `check_answers` 역참조 없음.

- [ ] **Step 1: 실패하는 회귀 테스트를 먼저 쓴다** (Review Focus 2·3)

`DecisionModelTests`에 넣는다.

```python
def test_quiz_models_and_tables_are_gone(self):
    from django.apps import apps
    for name in ("CheckQuestion", "Answer"):
        with self.assertRaises(LookupError, msg=name):
            apps.get_model("meetalign", name)
    names = connection.introspection.table_names()
    self.assertNotIn("meetalign_checkquestion", names)
    self.assertNotIn("meetalign_answer", names)

def test_meeting_has_no_quiz_transcript_field_and_still_saves(self):
    self.assertNotIn("check_questions_transcript", [f.name for f in Meeting._meta.get_fields()])
    self.meeting.transcript = "전사"
    self.meeting.record = "회의록"
    self.meeting.processing_status = Meeting.STATUS_DONE
    self.meeting.save(update_fields=["transcript", "record", "processing_status"])
    self.meeting.refresh_from_db()
    self.assertEqual(self.meeting.record, "회의록")
```

두 번째 테스트가 Review Focus 3을 고정한다 — `tasks.py:41`이 쓰는 그 `update_fields` 조합이 삭제 후에도 FieldError 없이 돌아야 한다.

- [ ] **Step 2: 실패를 확인한다**

Run: `python manage.py test meetalign.tests.DecisionModelTests -v 2`
Expected: 두 테스트 FAIL — 모델이 아직 있어 `LookupError`가 안 나고, `check_questions_transcript`가 필드 목록에 있다.

- [ ] **Step 3: `models.py`에서 모델 2개 + 필드 1개 삭제**

- `43-45`: 주석 2줄 + `check_questions_transcript = models.TextField(...)` 삭제.
- `121-144`: `class CheckQuestion` 전체 삭제.
- `147-164`: `class Answer` 전체 삭제.
- `169-185` 결정 컴파일러 구획 주석에서 "기존 CheckQuestion/Answer 흐름과 완전히 분리된 새 테이블이며, 기존 모델은 건드리지 않는다"와 "(실험 기능)"을 삭제된 모델을 가리키지 않게 고친다.
- `277`의 "기존 CheckQuestion.evidence 검증과 같은 방식" → 퀴즈를 언급하지 않는 표현으로.
- `305`의 "기존 Answer처럼 수정할 수 있다" → 동일.

- [ ] **Step 4: 마이그레이션 생성**

Run: `python manage.py makemigrations meetalign`
Expected: `0021_...` 1개만 생성. 2개 이상 생기면 멈추고 보고한다.
생성된 파일에 `RemoveField(model_name='meeting', name='check_questions_transcript')`, `DeleteModel(name='Answer')`, `DeleteModel(name='CheckQuestion')`(+ 제약 제거 operation)만 들어 있어야 한다. **파일 내용을 손으로 고치지 않는다.**

- [ ] **Step 5: SQL 확인 — 다른 테이블이 바뀌면 멈춘다**

Run: `python manage.py sqlmigrate meetalign 0021`
Expected: `meetalign_answer` DROP, `meetalign_checkquestion` DROP, `meetalign_meeting`의 `check_questions_transcript` 제거(SQLite는 테이블 재생성 방식이라 `meetalign_meeting`이 `__old`/새 테이블로 오가는 것은 정상).
**위 3개 테이블 외의 테이블 이름이 나오면 중단하고 전체 SQL을 보고한다.** 결과 전문을 최종 보고에 첨부한다.

- [ ] **Step 6: 적용과 왕복 확인**

Run: `python manage.py migrate meetalign`
Expected: `Applying meetalign.0021_... OK`.

Run: `python manage.py migrate meetalign 0020 && python manage.py migrate meetalign`
Expected: 역방향·정방향 모두 에러 없이 통과(테이블 구조만 복구되고 데이터는 돌아오지 않는 것이 정상 — 로컬 시연 데이터라 승인된 사항).

- [ ] **Step 7: 테스트 통과 확인**

Run: `python manage.py test`
Expected: `Ran 325 tests` + `OK` (323 + Step 1의 2개).

- [ ] **Step 8: 커밋 ②**

```bash
git add APL_04/meetalign/models.py APL_04/meetalign/migrations/0021_*.py APL_04/meetalign/tests.py
git commit -m "모델: CheckQuestion·Answer·Meeting.check_questions_transcript 삭제 (마이그레이션 0021)"
```
`db.sqlite3`가 index에 올라가지 않았는지 `git status --short`로 확인한다.

---

### Task 4 — 문서·주석 정리 (commit ④)

**Files:**
- Modify: `README.md`
- Modify: `docs/소프트웨어_사양서_간략판.md`
- Modify: `docs/소프트웨어_사양서_상세판.md`
- Modify: `APL_04/meetalign/templates/meetalign/signup.html:13`
- Modify: `APL_04/meetalign/templates/meetalign/base.html:144`
- Modify: `APL_04/meetalign/decision_compiler.py:246, 291`

**Interfaces:**
- Consumes: Task 3 종료 시점의 green 325개.
- Produces: 전환 문구 통일 — "이해도 확인 문제/퀴즈" → **"결정 명세"**, "불일치 탐지/검증 결과" → **"결정 확인"**, "주최자" → 결정 컴파일러 문맥에서는 **"진행자"**.

> `★` = 승인 지시에 명시된 지점. 그 외는 0.4-(나)에서 보고한 추가 발견 지점.

#### 4-A. `README.md` (3곳)

| 줄 | 바꿀 내용 |
|---|---|
| 6 ★ | "모호한 표현을 찾아 객관식 이해도 확인 문제를 자동 생성, 주최자가 검토·수정 후 공개" → 회의 원문에서 실행 명세(누가·무엇을·언제까지·완료 기준)를 뽑아 모호한 항목에 경고를 달고, 진행자가 항목마다 확정한 뒤 공개한다는 서술 |
| 7 | "정답과 다른 보기를 고른 팀원이 있으면 불일치로 표시" → 팀원이 항목마다 [확인했어요]/[제 이해와 달랐어요]를 남기고 전원 응답 뒤 익명 집계로 어긋난 항목을 본다는 서술 |
| 14 ★ | "회의록 요약, 이해도 확인 문제 생성, 대화창 답변" → "회의록 요약, 결정 명세 추출, 대화창 답변" |

3행("서로 다르게 이해한 부분을 짚어주는")은 결정 확인으로도 참이므로 **유지**.

#### 4-B. `signup.html:13` ★

"녹음 하나로 회의록, 이해도 확인, 불일치 탐지까지 자동으로." → "녹음 하나로 회의록, 결정 명세, 결정 확인까지 자동으로."

#### 4-C. `docs/소프트웨어_사양서_간략판.md` (11곳)

| 줄 | 바꿀 내용 |
|---|---|
| 9 | "객관식 확인 문제를 내고, 정답과 다른 보기를 고른 팀원을" → 실행 명세 + 결정 확인 서술 |
| 11 | "회의록 요약과 문제 생성은" → "회의록 요약과 결정 명세 추출은" |
| 15 | "녹음 → 전사 → 회의록 → 확인 문제 → 불일치 표시" → "녹음 → 전사 → 회의록 → 결정 명세 → 결정 확인" |
| 20 | "4. 이해도 확인 문제: …" 항목을 결정 명세 추출(실행 명세·경고·진행자 확정·미정 이월)로 교체 |
| 21 | "5. 불일치 탐지: …" 항목을 결정 확인(항목별 확인/달랐음, 전원 응답 뒤 집계 공개)으로 교체 |
| 23 ★ | "확인 퀴즈 미완료 인원 배지" → "결정 확인 미완료 인원 배지" |
| 39 | mermaid 노드 "이해도 문제는 주최자가 검토한 뒤 공개" → "결정 명세는 진행자가 확정한 뒤 공개" |
| 42 | "요약·문제 생성도 고정 응답으로" → "요약·결정 명세 추출도 고정 응답으로" |
| 51 | 표의 "요약·문제 생성·대화" → "요약·결정 명세·대화" |
| 57 | "이해도 문제와 답변" → "결정 명세와 확인 기록" |
| 65 | "주최자 전용: 문제 검토·공개·추가 생성" → "진행자 전용: 결정 명세 검토·확정·공개" |
| 70 | "자동 테스트 181개" → Task 3에서 확인된 실제 수 |
| 87 | verification 이름 노출 한계 항목 **삭제**(퀴즈 삭제로 해소) |

77행("이전 단계에서 … 전사→요약→문제→대화→불일치 전체 파이프라인을 검증한 이력")은 **과거 검증 이력의 사실 기술이므로 고치지 않는다.**

#### 4-D. `docs/소프트웨어_사양서_상세판.md` (21곳)

| 줄 | 바꿀 내용 |
|---|---|
| 13 ★ | 용어 행 "이해도 질문(CheckQuestion)" **삭제** |
| 14 | 용어 행 "불일치(discrepancy)" **삭제** |
| 17 | 용어 행 "묶음(batch)" **삭제** |
| 18 | "고정 문항·전사 원문 그대로" → 고정 명세·전사 원문 그대로 |
| 27 | 팀원 권한에서 "이해도 질문 답변, 검증 결과 열람" → "결정 확인(항목별 확인/달랐음)" |
| 36 | "4. 팀원이 이해도 질문에 답한다. 팀 홈에는 아직 안 푼 인원 수가" → 결정 확인 기준으로 |
| 53 | mermaid 노드 — 4-C 39행과 같은 문구로 |
| 60 | "URL 19개" — 아래 URL 표를 고치면 참이 되므로 **유지** |
| 61 | 모델 수는 이번 범위 밖(0.4-다) — **유지**하고 진행사항.txt에 기록 |
| 65 ★ | 집계 행 `meetalign/quiz_status.py` → `meetalign/decision_status.py` / "결정 확인 미완료 인원 집계. 회의 수와 무관하게 쿼리 3회" |
| 74 | "이해도 문제는 … `check_questions_transcript`에 남겨" 단락 **삭제** |
| 88 | 기능 행 `F7 이해도 문제 생성` **삭제** |
| 89 | 기능 행 `F8 문제 검토·공개(주최자)` **삭제** |
| 90 | 기능 행 `F9 이해도 질문 답변` **삭제** |
| 91 | 기능 행 `F10 불일치 탐지(검증 결과)` **삭제** |
| 94 | `F13 미완료 인원 배지` → 결정 확인 기준(공개된 확정 명세를 다 확인하지 않은 참가자 수)으로 교체 |
| 109 ★ | 모델 표 `CheckQuestion` 행 **삭제** |
| 110 | 모델 표 `Answer` 행 **삭제** |
| 104 | `Meeting` 행 필드 목록에서 `check_questions_transcript(text)` **삭제** |
| 131-133 | URL 표 3행(`understanding` `question_review` `verification`) **삭제**, 그 자리에 결정 컴파일러 3행 추가 — `/meetings/<id>/decisions/`(decision_ack, 팀원), `/meetings/<id>/decisions/review/`(decision_review, 진행자), `/meetings/<id>/decisions/<did>/carryover/resolve/`(decision_carryover_resolve, 진행자). 이것으로 114행·60행의 "URL 19개"가 참이 된다 |
| 159 | "호출자의 폴백(고정 문항·원문 유지·안내 문구)" → "고정 명세·원문 유지·안내 문구" |
| 168 | "문제 JSON 검증(`_parse_mcq_json`)" 단락 **삭제** |
| 231 | "자동 테스트 181개" → 실제 수 |
| 235 | `PrototypeFlowTests` 행의 "이해도 질문 생성·답변·재제출, … 문제 검토·공개" 삭제, 개수를 실제 수로 |
| 238 | `LocalLLMTests` 행의 "문제 생성", "근거 검증" 삭제, 개수를 실제 수로 |
| 241 | `QuizPendingTests` 행 **삭제** |
| 243-244 | `LanguageFilterTests` 개수 갱신, `QuestionTimingTests` 행 **삭제** |
| 246 | `DiscrepancyTests` **삭제**, `MeetingSummaryTests`만 남김 |
| 247 | `FollowupTests` 개수 갱신 |
| 288 | 알려진 이슈 "권한 | 검증 결과 화면(verification)…" 행 **삭제** |
| 298 | 향후 과제 "verification 화면의 이름 노출 범위를…" **삭제** |

#### 4-E. 코드 주석 (동작 변경 없음)

- [ ] **Step 1: `base.html:144`**

`/* ---- 확인 퀴즈 미완료 배지 (안 읽은 메시지 배지와 같은 모양) ---- */` → `/* ---- 결정 확인 미완료 배지 (안 읽은 메시지 배지와 같은 모양) ---- */`
**CSS 선언은 한 글자도 바꾸지 않는다.**

- [ ] **Step 2: `decision_compiler.py`**

`:246` "기존 퀴즈 생성(services.generate_check_questions_from_text)과 같은 처리다." → 삭제된 함수를 가리키지 않는 표현으로(예: 근거가 원문에 실제로 있는지 공백 무시 비교로 확인한다는 서술만 남긴다).
`:291` "기존 _parse_mcq_json도 같은 규칙이다." → 삭제.

> `models.py`·`views.py`·`tests.py`의 주석은 각각 Task 3 Step 3, Task 2 Step 2, Task 1 Step 2에서 이미 처리된다.

- [ ] **Step 3: 주석 수정이 스키마를 건드리지 않았는지 확인**

Run: `python manage.py makemigrations --check --dry-run`
Expected: `No changes detected` (종료 코드 0). 새 마이그레이션이 감지되면 멈추고 보고한다.

- [ ] **Step 4: 테스트와 문서 잔여 참조 확인**

Run: `python manage.py test`
Expected: `Ran 325 tests` + `OK`.

Run: `grep -rn "퀴즈\|CheckQuestion\|quiz_status" README.md docs/소프트웨어_사양서_간략판.md docs/소프트웨어_사양서_상세판.md APL_04/meetalign/templates/`
Expected: 출력 없음. (`docs/plans/`는 과거 계획 문서이므로 대상에서 제외한다.)

- [ ] **Step 5: 커밋 ④**

```bash
git add README.md docs/소프트웨어_사양서_간략판.md docs/소프트웨어_사양서_상세판.md \
        APL_04/meetalign/templates/meetalign/signup.html \
        APL_04/meetalign/templates/meetalign/base.html \
        APL_04/meetalign/decision_compiler.py
git commit -m "문서·주석: 퀴즈 서술을 결정 명세·결정 확인 기준으로 전환"
```

---

### Task 5 — 진행사항.txt + 최종 전수 검증 (commit ⑤)

**Files:**
- Modify: `진행사항.txt`

**Interfaces:**
- Consumes: Task 4 종료 시점의 green 325개.
- Produces: 없음(최종 태스크).

- [ ] **Step 1: 새 항목을 파일 끝에 추가한다**

제목: `퀴즈 기능 완전 삭제 (브랜치 chore/remove-quiz)`

담아야 할 내용:
- 삭제한 것: 모델 2개 + `Meeting.check_questions_transcript`, 마이그레이션 0021, 뷰 5개, URL 3개, 템플릿 3개, `quiz_status.py`, `services.py` 퀴즈 전용 4개 심볼, 테스트 52개.
- 남긴 것과 이유: `answers`/`inbox`(익명 질문함은 퀴즈가 아님), `base.html` `.pending-badge` CSS(결정 확인 배지가 사용), `services._generate_clean`·`_normalize_for_match`·`_enabled`·`_is_korean_or_english`(결정 컴파일러가 직접 호출).
- 테스트 수: `372 → 325` — 퀴즈 전용 52개 삭제, 회귀 5개 추가(옛 URL 404, URL name 미해결, 익명 질문함 생존, 퀴즈 모델·테이블 부재, `update_fields` 저장). 퀴즈 무관 테스트는 하나도 줄지 않았고, `_generate_clean` 재시도 검증 2개는 `summarize` 경유로 옮겨 커버리지를 유지했다.
- **알려진 이슈 "퀴즈 삭제로 해결" 표시** (지시 사항):
  - `question_review`의 미완료 참가자 명단이 `host`가 비면 팀원 누구에게나 보이던 문제(기존 601-604행) → **퀴즈 삭제로 해결**(화면 자체가 없어짐).
  - `verification`(3.6)을 팀원 누구나 볼 수 있어 다른 팀원 이름과 고른 보기가 노출되던 문제(기존 605행~) → **퀴즈 삭제로 해결**.
  - 추가 문장(지시 사항): `엇갈린 답변(verification)은 결정 확인의 '제 이해와 달랐어요' 익명 집계로 대체됨 (PR #38, #40)`
- 새로 남는 알려진 이슈: `docs/소프트웨어_사양서_상세판.md`의 모델 표·모델 수는 결정 컴파일러(PR #38) 도입분이 아직 반영되지 않았다. 이번 범위는 퀴즈 서술 제거까지이며 현행화는 별도 작업으로 남긴다.

- [ ] **Step 2: 최종 전수 검증 1 — 퀴즈 참조 그렙**

Run: `grep -rn "CheckQuestion\|check_question\|quiz_status\|understanding\|verification" APL_04/`
Expected: `APL_04/meetalign/migrations/` 안의 과거 파일(`0002`, `0003`, `0010`, `0016`, `0018`, `0021`)만 나온다. 그 밖의 경로가 한 줄이라도 나오면 멈추고 보고한다. (`.venv/`가 섞이면 `--exclude-dir=.venv`를 붙인다.)

- [ ] **Step 3: 최종 전수 검증 2 — 마이그레이션 상태**

Run: `python manage.py makemigrations --check --dry-run`
Expected: `No changes detected`.

Run: `python manage.py migrate --plan | tail -5`
Expected: 0021이 마지막이고 미적용 마이그레이션이 없다.

- [ ] **Step 4: 최종 전수 검증 3 — 테스트**

Run: `python manage.py test`
Expected: `Ran 325 tests` + `OK`.
**예측(372 → 320 + 회귀 5 = 325)과 실제가 다르면, 차이를 테스트 이름 단위로 설명한다.** 특히 "퀴즈와 무관한 테스트가 줄지 않았다"를 Task 1의 52개 목록과 대조해 입증한다.

- [ ] **Step 5: 커밋 ⑤**

```bash
git add 진행사항.txt
git commit -m "진행사항: 퀴즈 기능 완전 삭제 요약과 해결된 알려진 이슈 정리"
```

- [ ] **Step 6: 최종 상태 보고 (push 하지 않는다)**

보고에 포함할 것:
- `git log --oneline -6`, `git status --short --branch`
- `python manage.py sqlmigrate meetalign 0021` **전문**
- Step 2 그렙 결과 전문
- `Ran 325 tests ... OK`
- 테스트 수 설명: 삭제된 퀴즈 테스트 52개 / 남은 테스트 320개 / 추가한 회귀 5개 = 325개

`git push`는 하지 않는다 — 사용자가 직접 한다.

---

## 자기 점검 결과

- **범위 커버리지:** 0.2의 모든 항목이 Task 1~3에, 지시 (1)문서가 Task 4에, 지시 (2)주석이 Task 4-E + Task 1~3의 각 주석 스텝에, 추가 검증 3건이 Task 3 Step 5(`sqlmigrate`)·Task 5 Step 2(그렙)·Task 5 Step 4(테스트 수 차이 설명)에 배치됨. 커밋 5분할은 Task 1~5에 1:1 대응(순서는 0.4-가에서 보고한 사유로 재배치).
- **타입·이름 일관성:** `decision_status.pending_count_for_meetings` / `badge_text`는 기존 코드의 실제 이름이며 삭제 대상이 아님을 재확인. `NoReverseMatch` import 추가를 Task 2 Step 5에 명시.
- **테스트 수 추적:** 372 → (Task 1) 320 → (Task 2 +3) 323 → (Task 3 +2) 325. 각 태스크의 Expected에 숫자를 박아 두어 중간에 어긋나면 바로 드러난다.
- **Review Focus 커버리지:** 5개 항목 전부 담당 태스크에 테스트가 붙었다(1→Task 2 Step 5, 2→Task 3 Step 5·6, 3→Task 3 Step 1, 4→Task 1 Step 1, 5→Task 2 Step 5).
- **미결 1건:** 0.4의 (가)·(나)·(다)는 사용자 승인 지시의 문자 그대로에서 벗어나는 판단이므로 계획 승인 시 확인이 필요하다.
