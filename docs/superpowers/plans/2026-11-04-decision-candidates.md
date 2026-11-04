# 확정 문장 후보 재설계 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** AI가 모호한 결정을 찾아 그대로 공개될 확정 문장 후보 2~3개를 만들고, 진행자가 읽고 탭 한 번으로 고르는 흐름으로 결정 컴파일러의 확정 단계를 바꾼다.

**Architecture:** 전사 원문을 LLM에 한 번 보내 결정·모호 여부·이유·후보·근거·추천을 한 응답으로 받는다. 규칙은 안전망으로 축소해 명확 판정 항목의 확정 문장과 원문 근거만 스캔한다. 공개되는 문장은 모든 상태에서 `Decision.final_text` 하나로 통일한다. 진행자 화면의 명세 입력 칸을 없애고 라디오 선택만 남긴다.

**Tech Stack:** Django 5 (SQLite), 표준 `unittest` 기반 `django.test.TestCase`, LLM 호출은 `services._generate_clean`/`_enabled`/`_normalize_for_match` 재사용, 템플릿은 Django 템플릿 + 기존 디자인 토큰.

**Spec:** `docs/superpowers/specs/2026-11-04-decision-candidates-design.md`

## Global Constraints

- `APL_04/meetalign/services.py`는 수정하지 않는다. `_generate_clean(prompt)`, `_normalize_for_match(text)`, `_enabled()`만 호출해 쓴다.
- 새 CSS 프레임워크·CDN 금지. 기존 토큰(`card elevated`, `badge`, `record`, `meta`, `label`, `success`, `notice`, `item`, `plain`)만 쓴다.
- 톤: "틀렸다"가 아니라 "다르게 이해할 수 있다". 화면 문구는 "확인 필요"를 쓰고 "경고"를 쓰지 않는다(모델 필드명 `warnings`는 유지).
- 마이그레이션은 `0022` 하나만 추가한다. 기존 필드를 `RemoveField`로 지우지 않는다.
- 상수: `MAX_DECISIONS = 12`, `MAX_INTERPRETATIONS = 3`, `PARSE_RETRIES = 1`. `MIN_INTERPRETATIONS`·`MAX_WARNED_DECISIONS`는 삭제한다.
- 테스트 명령은 `APL_04`에서 `python manage.py test meetalign`. 각 Task 끝에 `Ran N tests` 숫자를 확인한다.
- `git add`는 파일을 지정해서 한다. `push`는 하지 않는다. 커밋 메시지는 한국어로 쓰고 끝에 `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`를 붙인다.

## Review Focus

스펙이 전제하지만 어느 Task의 주 테스트도 건드리지 않는 입력들이다. 다섯 건 모두 **새 테스트 함수가 아니라 소유 Task의 신규 테스트 안 추가 assertion**으로 고정한다(총계 331을 지키기 위함).

1. `candidates`가 객체 배열이 아니라 문자열 배열(`["문장1", "문장2"]`)로 온다 → 그 후보들을 조용히 버리고 예외를 올리지 않는다. (Task 3 / `test_parse_reads_the_new_schema`)
2. `recommended`가 `"true"`·`1` 같은 비불리언으로 온다 → 참으로 보고 배지를 붙인다. (Task 3 / `test_only_one_candidate_is_recommended`)
3. `ambiguous:true`인데 `final`도 함께 온다 → `candidates`를 쓰고 `final`은 무시한다. (Task 3 / `test_parse_reads_the_new_schema`)
4. `topic`이 200자를 넘게 온다 → 저장 시 120자로 자른다(카드 제목이 레이아웃을 깨지 않게). (Task 3 / `test_parse_drops_items_without_a_topic`)
5. 진행자가 모든 항목을 '이 결정 빼기'로 지워 draft가 0개가 된다 → 공개 버튼이 막히고 기존 "공개할 항목이 없습니다" 사유가 나온다. (Task 7 / `test_dropping_every_item_leaves_nothing_to_publish`)

---

### Task 1: 마이그레이션 0022와 모델 계약

**Files:**
- Create: `APL_04/meetalign/migrations/0022_decision_candidates.py`
- Modify: `APL_04/meetalign/models.py:136-245` (Decision, Interpretation)
- Modify: `APL_04/meetalign/admin.py:16-20`
- Modify: `APL_04/meetalign/templates/meetalign/meeting_detail.html:39`
- Test: `APL_04/meetalign/tests.py` (`DecisionModelTests`)

**Interfaces:**
- Consumes: 없음 (첫 Task)
- Produces: `Decision.divergence_reason: str`, `Interpretation.recommended: bool`,
  `Decision.published_text -> str` (폴백 없음), `Decision.is_settled -> bool`,
  `Decision.has_warning -> bool`,
  `migrations.0022_decision_candidates.spec_sentence(who: str, what: str, due: str, done: str) -> str`

- [ ] **Step 1: 실패하는 테스트를 쓴다** — `DecisionModelTests`에 추가하고, `spec_line`·폴백 테스트 2개를 지운다

```python
# 삭제: test_spec_line_shows_blank_fields_as_question_marks
# 삭제: test_published_text_falls_back_to_the_spec_line

def test_0022_builds_a_sentence_from_the_old_fields(self):
    from importlib import import_module
    spec_sentence = import_module("meetalign.migrations.0022_decision_candidates").spec_sentence
    self.assertEqual(spec_sentence("민수", "배포", "금요일", "확인"), "민수 · 배포 · 금요일 · 확인")
    self.assertEqual(spec_sentence("", "배포", "", ""), "배포")

# 재작성: test_is_settled_requires_a_sentence_or_the_undecided_mark 에 divergence_reason 사례 추가
def test_an_item_with_only_a_divergence_reason_is_not_settled(self):
    decision = Decision.objects.create(
        meeting=self.meeting, order=7, what="로그 보관 기간",
        divergence_reason="\"충분히\"가 30일인지 90일인지 갈립니다",
    )
    self.assertFalse(decision.is_settled)
    self.assertTrue(decision.has_warning)
    decision.final_text = "민수가 12월 5일까지 30일로 바꾼다"
    self.assertTrue(decision.is_settled)
    self.assertEqual(decision.published_text, decision.final_text)
```

- [ ] **Step 2: 실패를 확인한다**

Run: `cd APL_04 && python manage.py test meetalign.tests.DecisionModelTests -v 2`
Expected: FAIL — `ModuleNotFoundError: meetalign.migrations.0022_decision_candidates`, `TypeError: Decision() got unexpected keyword 'divergence_reason'`

- [ ] **Step 3: 모델과 마이그레이션을 구현한다**

`models.py`:
- `Decision.divergence_reason = models.TextField(blank=True, default="")` — AI가 쓴 "왜 갈릴 수 있나" 한 줄
- `Interpretation.recommended = models.BooleanField(default=False)` — 'AI 추천' 배지
- `Decision.has_warning` → `bool(self.warnings or self.divergence_reason)`
- `Decision.is_settled` → `bool(self.final_text) or self.marked_undecided or not self.has_warning`
- `Decision.published_text` → `self.final_text` (폴백 삭제)
- `Decision.spec_line` 프로퍼티 삭제. `Interpretation` docstring을 "그대로 공개될 확정 문장 후보"로 고친다.

`admin.py`: `list_display`·`search_fields`의 `who` → `final_text`.

`meeting_detail.html:39`: `{{ decision.spec_line }}` → `{{ decision.final_text }}`.

`migrations/0022_decision_candidates.py`: `AddField` 2개 + `RunPython`.
`spec_sentence`는 빈칸을 건너뛰고 `" · "`로 잇는다. `RunPython`은 `status`가 `published`/`undecided`이고 `final_text`가 빈 레코드에만 `spec_sentence(...) or what`을 채운다. 역방향은 `migrations.RunPython.noop`.

- [ ] **Step 4: 통과를 확인한다**

Run: `cd APL_04 && python manage.py makemigrations --check --dry-run meetalign && python manage.py test meetalign -v 1`
Expected: 마이그레이션 누락 없음, `Ran 324 tests` / FAILED는 없음 — 단, 이 시점에 `spec_line`을 참조하던 다른 테스트가 걸리면 그 테스트도 `final_text` 기준으로 고친다.

- [ ] **Step 5: 커밋**

```bash
git add APL_04/meetalign/models.py APL_04/meetalign/admin.py APL_04/meetalign/migrations/0022_decision_candidates.py APL_04/meetalign/templates/meetalign/meeting_detail.html APL_04/meetalign/tests.py
git commit -m "모델: divergence_reason·recommended 칸과 final_text 단일화 (마이그레이션 0022)"
```

**Task 1 종료 시 테스트 수: 324** (325 − 삭제 2 + 신규 1)

---

### Task 2: 미정 항목의 자동 문장과 공개 문장 통일

**Files:**
- Modify: `APL_04/meetalign/decision_compiler.py` (`undecided_sentence` 추가)
- Modify: `APL_04/meetalign/views.py:366-368` (`_apply_host_choice`의 undecided 분기)
- Modify: `APL_04/meetalign/templates/meetalign/decision_ack.html:27`
- Test: `APL_04/meetalign/tests.py` (`DecisionConfirmTests`)

**Interfaces:**
- Consumes: Task 1의 `Decision.published_text`
- Produces: `decision_compiler.undecided_sentence(topic: str) -> str`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
def test_marking_undecided_writes_a_sentence_of_its_own(self):
    decision = self.warned[0]         # setUp이 만든 확인 필요 항목
    self.client.post(self.url, {"action": "save",
                                "choice_%d" % decision.id: decision_compiler.CHOICE_UNDECIDED})
    decision.refresh_from_db()
    self.assertTrue(decision.marked_undecided)
    self.assertEqual(
        decision.final_text,
        "%s — 이번 회의에서 결론이 나지 않았음 (다음 회의로)" % decision.what,
    )
    self.assertEqual(decision.published_text, decision.final_text)
    # 팀원 화면이 같은 문장을 쓴다
    self._confirm_all()
    self.client.post(self.url, {"action": "publish"})
    member = Client()
    member.force_login(self.member)
    self.assertContains(member.get(reverse("decision_ack", args=[self.meeting.id])),
                        "이번 회의에서 결론이 나지 않았음")
```

- [ ] **Step 2: 실패를 확인한다**

Run: `cd APL_04 && python manage.py test meetalign.tests.DecisionConfirmTests.test_marking_undecided_writes_a_sentence_of_its_own -v 2`
Expected: FAIL — `final_text`가 `""`

- [ ] **Step 3: 구현한다**

`decision_compiler.undecided_sentence(topic)` → `"%s — 이번 회의에서 결론이 나지 않았음 (다음 회의로)" % topic`.
`views._apply_host_choice`의 `CHOICE_UNDECIDED` 분기에서 `decision.final_text = decision_compiler.undecided_sentence(decision.what)`로 채운다(기존의 `= ""`를 교체).
`decision_ack.html:27`의 `{% if ... status_published %}{{ published_text }}{% else %}{{ what }}{% endif %}` 분기를 없애고 `{{ row.decision.published_text }}` 하나로 만든다. 기존 주석("미결정 항목은 확정 문장이 없어…")도 지운다.

- [ ] **Step 4: 통과를 확인한다**

Run: `cd APL_04 && python manage.py test meetalign -v 1`
Expected: `Ran 325 tests`, FAILED 없음

- [ ] **Step 5: 커밋**

```bash
git add APL_04/meetalign/decision_compiler.py APL_04/meetalign/views.py APL_04/meetalign/templates/meetalign/decision_ack.html APL_04/meetalign/tests.py
git commit -m "미정 항목도 확정 문장을 갖는다 — 공개 문장을 final_text 하나로 통일"
```

**Task 2 종료 시 테스트 수: 325** (324 + 신규 1)

---

### Task 3: 통합 응답 파서

**Files:**
- Modify: `APL_04/meetalign/decision_compiler.py` (`parse_decisions` 교체, `parse_interpretations` 흡수)
- Test: `APL_04/meetalign/tests.py` (`DecisionParseTests`)

**Interfaces:**
- Consumes: 없음 (순수 함수)
- Produces: `decision_compiler.parse_decisions(raw: str, source_text: str | None = None) -> list[dict]`,
  항목 모양은
  `{"topic": str, "ambiguous": bool, "why": str, "final": str, "evidence": str, "candidates": [{"text": str, "evidence": str, "recommended": bool}]}`

- [ ] **Step 1: 실패하는 테스트를 쓴다** — `DecisionParseTests`를 새 스키마로 재작성하고 `test_parse_decisions_keeps_blank_who_and_due`를 지운다

```python
# 삭제: test_parse_decisions_keeps_blank_who_and_due
# 재작성(이름 유지 가능): 코드펜스 견디기, 깨진 JSON → [], MAX_DECISIONS 자르기,
#                        중복 후보 제거, 근거 불일치 폐기, 공백 무시 비교, 빈 evidence 건너뛰기

TOPIC = "로그 보관 기간"

def test_parse_reads_the_new_schema(self):
    raw = json.dumps([
        {"topic": TOPIC, "ambiguous": True, "why": "30일인지 90일인지 갈립니다",
         "final": "무시되어야 한다",
         "candidates": [{"text": "30일로 바꾼다", "evidence": "", "recommended": True},
                        {"text": "90일로 바꾼다", "evidence": ""}]},
        {"topic": "후보가 문자열", "ambiguous": True, "why": "x",
         "candidates": ["문장만 왔다", "이것도"]},
    ], ensure_ascii=False)
    items = decision_compiler.parse_decisions(raw)
    self.assertEqual(items[0]["topic"], TOPIC)
    self.assertTrue(items[0]["ambiguous"])
    self.assertEqual([c["text"] for c in items[0]["candidates"]], ["30일로 바꾼다", "90일로 바꾼다"])
    self.assertEqual(items[0]["final"], "")          # ambiguous면 final은 버린다
    self.assertEqual(items[1]["candidates"], [])     # 문자열 후보는 조용히 버린다

def test_only_one_candidate_is_recommended(self):
    raw = json.dumps([{"topic": TOPIC, "ambiguous": True, "why": "x", "candidates": [
        {"text": "첫째", "recommended": "true"},     # 비불리언도 참으로 본다
        {"text": "둘째", "recommended": True},
    ]}], ensure_ascii=False)
    flags = [c["recommended"] for c in decision_compiler.parse_decisions(raw)[0]["candidates"]]
    self.assertEqual(flags, [True, False])

def test_parse_drops_items_without_a_topic(self):
    raw = json.dumps([{"topic": "", "ambiguous": False, "final": "문장"},
                      {"topic": "가" * 300, "ambiguous": False, "final": "문장"}],
                     ensure_ascii=False)
    items = decision_compiler.parse_decisions(raw)
    self.assertEqual(len(items), 1)
    self.assertEqual(len(items[0]["topic"]), 120)    # 긴 topic은 자른다

def test_a_clear_item_with_evidence_absent_from_the_transcript_loses_its_sentence(self):
    source = "로그는 일단 한 달 정도면 충분할 것 같은데요"
    raw = json.dumps([{"topic": TOPIC, "ambiguous": False,
                       "final": "민수가 30일로 바꾼다", "evidence": "회의에 없던 문장"}],
                     ensure_ascii=False)
    item = decision_compiler.parse_decisions(raw, source_text=source)[0]
    self.assertEqual(item["final"], "")
```

- [ ] **Step 2: 실패를 확인한다**

Run: `cd APL_04 && python manage.py test meetalign.tests.DecisionParseTests -v 2`
Expected: FAIL — `KeyError: 'topic'` (기존 파서는 `who`/`what` 스키마를 읽는다)

- [ ] **Step 3: `parse_decisions(raw, source_text=None)`를 구현한다**

`_json_array(raw)`는 그대로 쓴다. 항목마다:
`topic`이 비면 버리고, 120자로 자른다. `ambiguous`는 `bool(...)`. `ambiguous`가 참이면 `final`/`evidence`를 빈 문자열로 만들고 `candidates`를 읽는다; 거짓이면 `candidates`를 비우고 `final`/`evidence`를 읽는다.
후보는 `dict`가 아니면 건너뛰고, `text`가 비면 건너뛰고, 같은 `text`는 한 번만 받고, `MAX_INTERPRETATIONS`에서 자른다. `recommended`는 `bool(...)`로 읽어 참인 것 중 **첫 번째만** 남기고 나머지는 `False`로 되돌린다.
`source_text`가 주어지면 `services._normalize_for_match`로 근거를 검증한다 — 후보는 검증 실패 시 그 후보를 버리고, 명확 항목은 `final`을 빈 문자열로 만든다(승격은 Task 5가 한다). `evidence`가 빈 문자열이면 검증을 건너뛴다(가짜 응답 모드 통로).
항목 수는 `MAX_DECISIONS`에서 자른다. `parse_interpretations`는 삭제하고 후보 검증을 이 함수 안으로 옮긴다.

- [ ] **Step 4: 통과를 확인한다**

Run: `cd APL_04 && python manage.py test meetalign.tests.DecisionParseTests -v 1`
Expected: OK. 전체는 Task 5까지 깨진 상태로 둔다(`compile_meeting`이 아직 옛 스키마를 쓴다) — 이 Task에서는 `DecisionParseTests`만 통과시키고, 전체 숫자는 Task 5 끝에서 맞춘다.

- [ ] **Step 5: 커밋**

```bash
git add APL_04/meetalign/decision_compiler.py APL_04/meetalign/tests.py
git commit -m "파서: 결정·모호 여부·이유·후보·근거·추천을 한 응답에서 읽는다"
```

**Task 3 종료 시 테스트 수: 325** (325 − 삭제 1 + 신규 1)

---

### Task 4: 규칙 안전망 축소

**Files:**
- Modify: `APL_04/meetalign/decision_compiler.py:85-200` (`warnings_for`·`_blank_warnings`·`_cross_warnings` 삭제, `safety_warnings` 추가)
- Test: `APL_04/meetalign/tests.py` (`DecisionWarningTests`)

**Interfaces:**
- Consumes: 없음 (순수 함수)
- Produces: `decision_compiler.safety_warnings(final_text: str, evidence: str) -> list[str]`

- [ ] **Step 1: 실패하는 테스트를 쓴다** — 빈칸 2개·`_cross` 3개·상한 2개를 지우고, 남는 테스트는 `safety_warnings` 호출로 재작성한다

```python
# 삭제: test_blank_fields_each_raise_their_own_warning, test_fully_filled_spec_has_no_warning,
#       test_same_owner_with_different_due_dates_warns_on_both_items,
#       test_same_owner_with_the_same_due_date_does_not_warn,
#       test_different_owners_are_not_compared_with_each_other,
#       test_at_most_five_items_keep_their_warnings,
#       test_items_without_warnings_do_not_use_up_the_five_slots
# 재작성: test_each_ambiguous_phrase_is_detected, test_two_dates_in_one_spec_raise_a_conflict_warning,
#         test_a_single_date_written_as_month_and_day_is_not_a_conflict,
#         test_two_quantities_in_one_spec_raise_a_conflict_warning,
#         test_the_same_warning_reason_is_not_listed_twice
#         → 모두 safety_warnings(final_text, evidence) 를 부른다

def test_an_ambiguous_phrase_in_the_sentence_is_caught(self):
    self.assertEqual(
        decision_compiler.safety_warnings("나중에 로깅 기준을 맞춘다", "로깅은 맞춰야죠"),
        ["확정 문장에 모호한 표현이 있어요: '나중에'"],
    )

def test_an_ambiguous_phrase_only_in_the_transcript_is_caught(self):
    # 원문은 모호한데 AI가 혼자 해소한 경우 — 진행자가 확인해야 한다
    self.assertEqual(
        decision_compiler.safety_warnings("민수가 다음 주 금요일까지 배포한다", "다음 주쯤 할게요"),
        ["원문에 모호한 표현이 있어요: '쯤'"],
    )

def test_the_same_phrase_on_both_sides_is_listed_once(self):
    found = decision_compiler.safety_warnings("나중에 맞춘다", "나중에 맞추죠")
    self.assertEqual(found, ["확정 문장에 모호한 표현이 있어요: '나중에'"])

def test_conflicting_dates_are_caught_on_each_side(self):
    self.assertEqual(
        decision_compiler.safety_warnings("금요일 아니면 다음 주 월요일에 배포한다", ""),
        ["확정 문장에 다른 날짜가 섞여 있어요: 금요일, 월요일"],
    )
```

- [ ] **Step 2: 실패를 확인한다**

Run: `cd APL_04 && python manage.py test meetalign.tests.DecisionWarningTests -v 2`
Expected: FAIL — `AttributeError: module 'meetalign.decision_compiler' has no attribute 'safety_warnings'`

- [ ] **Step 3: `safety_warnings(final_text, evidence)`를 구현한다**

`AMBIGUOUS_PHRASES`·`_DATE_EXPR`·`_QUANTITY_EXPR`·`_date_tokens`·`_quantity_tokens`는 그대로 쓴다.
한 텍스트에 대해 모호어와 날짜·숫자 충돌을 찾는 내부 함수를 하나 두고, 그것을 `final_text`와 `evidence`에 각각 적용한다. 사유 문구는 접두사만 다르다 — `확정 문장에 …` / `원문에 …`. 정확한 문구 네 가지:

```
확정 문장에 모호한 표현이 있어요: '%s'
원문에 모호한 표현이 있어요: '%s'
확정 문장에 다른 날짜가 섞여 있어요: %s      # 토큰을 sorted()로 ", " 조인
확정 문장에 다른 숫자가 섞여 있어요: %s      # 원문 쪽도 같은 형식
```

순서는 확정 문장 사유 먼저, 원문 사유 다음. 같은 모호어가 양쪽에서 걸리면 확정 문장 쪽만 남긴다. 중복 제거는 기존 `warnings_for`의 방식(순서 유지하며 처음 것만)을 쓴다. `warnings_for`·`_blank_warnings`·`_cross_warnings`는 삭제한다.

- [ ] **Step 4: 통과를 확인한다**

Run: `cd APL_04 && python manage.py test meetalign.tests.DecisionWarningTests -v 1`
Expected: OK (전체 스위트는 Task 5 끝에서 맞춘다)

- [ ] **Step 5: 커밋**

```bash
git add APL_04/meetalign/decision_compiler.py APL_04/meetalign/tests.py
git commit -m "규칙 안전망: 빈칸·담당자 교차 검사를 버리고 확정 문장과 원문 근거만 본다"
```

**Task 4 종료 시 테스트 수: 321** (325 − 삭제 7 + 신규 3)

---

### Task 5: `compile_meeting` — 호출 한 번과 승격

**Files:**
- Modify: `APL_04/meetalign/decision_compiler.py` (`_compile_prompt`, `compile_specs`, `compile_meeting`, `FAKE_DECISIONS`; `_interpretation_prompt`·`interpretations_for`·`_fake_interpretations` 삭제)
- Test: `APL_04/meetalign/tests.py` (`DecisionCompileTests`)

**Interfaces:**
- Consumes: Task 3의 `parse_decisions`, Task 4의 `safety_warnings`, Task 1의 모델 칸
- Produces: `decision_compiler.compile_meeting(meeting) -> {"decisions": int, "ambiguous": int, "few_candidates": int}`,
  `decision_compiler.compile_specs(source: str) -> list[dict]`

- [ ] **Step 1: 실패하는 테스트를 쓴다** — `test_dropped_warning_count_is_reported`를 지우고, `fake_mode`·`compile_uses_the_llm_response`를 새 스키마로 재작성한다

```python
# 삭제: test_dropped_warning_count_is_reported

#   setUp의 transcript를 아래로 바꾼다 — 모든 테스트가 이 원문 안에서 근거를 찾는다.
#   SOURCE = ("로그는 일단 한 달 정도면 충분할 것 같은데요. "
#             "배포 스크립트는 제가 금요일까지 정리할게요. "
#             "QA는 다음 주쯤 할게요.")

def _compiled(self, *items):
    """한 응답으로 컴파일하고 요약과 {topic: Decision} 을 돌려준다."""
    raw = json.dumps(list(items), ensure_ascii=False)
    with patch.dict(os.environ, {"OLLAMA_MODEL": "m1"}), \
         patch.object(services, "_generate", return_value=raw) as gen:
        summary = decision_compiler.compile_meeting(self.meeting)
    return summary, {d.what: d for d in self.meeting.decisions.all()}, gen

CLEAR = {"topic": "배포 스크립트 정리", "ambiguous": False,
         "final": "민수가 금요일까지 배포 스크립트를 정리한다",
         "evidence": "배포 스크립트는 제가 금요일까지 정리할게요."}
AMBIGUOUS = {"topic": "로그 보관 기간", "ambiguous": True,
             "why": "30일인지 90일인지 갈립니다",
             "candidates": [{"text": "민수가 30일로 바꾼다",
                             "evidence": "로그는 일단 한 달 정도면 충분할 것 같은데요.",
                             "recommended": True},
                            {"text": "민수가 90일로 바꾼다",
                             "evidence": "로그는 일단 한 달 정도면 충분할 것 같은데요."}]}

def test_the_llm_is_called_exactly_once(self):
    _, _, gen = self._compiled(self.CLEAR, self.AMBIGUOUS)
    self.assertEqual(gen.call_count, 1)

def test_a_clear_item_is_published_without_a_tap(self):
    _, by_topic, _ = self._compiled(self.CLEAR)
    decision = by_topic["배포 스크립트 정리"]
    self.assertEqual(decision.final_text, "민수가 금요일까지 배포 스크립트를 정리한다")
    self.assertEqual(decision.interpretations.count(), 0)
    self.assertEqual(decision.warnings, [])
    self.assertEqual(decision.divergence_reason, "")
    self.assertTrue(decision.is_settled)

def test_an_ambiguous_item_waits_for_the_host(self):
    _, by_topic, _ = self._compiled(self.AMBIGUOUS)
    decision = by_topic["로그 보관 기간"]
    self.assertEqual(decision.divergence_reason, "30일인지 90일인지 갈립니다")
    self.assertEqual(decision.final_text, "")
    self.assertFalse(decision.is_settled)
    self.assertEqual([i.recommended for i in decision.interpretations.all()], [True, False])

def test_a_rule_hit_on_the_sentence_promotes_a_clear_item(self):
    item = dict(self.CLEAR, final="나중에 배포 스크립트를 정리한다")
    _, by_topic, _ = self._compiled(item)
    decision = by_topic["배포 스크립트 정리"]
    self.assertEqual(decision.warnings, ["확정 문장에 모호한 표현이 있어요: '나중에'"])
    self.assertEqual([i.text for i in decision.interpretations.all()],
                     ["나중에 배포 스크립트를 정리한다"])
    self.assertEqual(decision.final_text, "")
    self.assertFalse(decision.is_settled)

def test_a_rule_hit_on_the_transcript_promotes_a_clear_item(self):
    item = {"topic": "QA 범위", "ambiguous": False,
            "final": "민수가 다음 주 금요일까지 QA를 끝낸다",
            "evidence": "QA는 다음 주쯤 할게요."}
    _, by_topic, _ = self._compiled(item)
    decision = by_topic["QA 범위"]
    self.assertEqual(decision.warnings, ["원문에 모호한 표현이 있어요: '쯤'"])
    self.assertFalse(decision.is_settled)

def test_the_rules_do_not_scan_candidates_of_an_ambiguous_item(self):
    item = dict(self.AMBIGUOUS, candidates=[
        dict(self.AMBIGUOUS["candidates"][0], text="나중에 30일로 바꾼다"),
        self.AMBIGUOUS["candidates"][1]])
    _, by_topic, _ = self._compiled(item)
    decision = by_topic["로그 보관 기간"]
    self.assertEqual(decision.warnings, [])
    self.assertTrue(decision.divergence_reason)

def test_a_clear_item_without_usable_evidence_is_promoted(self):
    item = dict(self.CLEAR, evidence="회의에 없던 문장입니다.")
    _, by_topic, _ = self._compiled(item)
    decision = by_topic["배포 스크립트 정리"]
    self.assertEqual(decision.final_text, "")
    self.assertEqual(decision.interpretations.count(), 0)
    self.assertTrue(decision.has_warning)

def test_one_broken_item_does_not_take_the_others_down(self):
    broken = {"topic": "회의록 공유", "ambiguous": True, "why": "범위가 갈립니다",
              "candidates": [{"text": "A", "evidence": "회의에 없던 문장입니다."},
                             {"text": "B", "evidence": "이것도 없습니다."}]}
    summary, by_topic, _ = self._compiled(self.CLEAR, self.AMBIGUOUS, broken)
    self.assertEqual(summary["decisions"], 3)
    self.assertEqual(summary["few_candidates"], 1)
    self.assertEqual(by_topic["회의록 공유"].interpretations.count(), 0)
    self.assertTrue(by_topic["배포 스크립트 정리"].final_text)

def test_eight_ambiguous_items_all_wait_for_the_host(self):
    items = [dict(self.AMBIGUOUS, topic="안건 %d" % i) for i in range(8)]
    summary, _, _ = self._compiled(*items)
    self.assertEqual(summary["ambiguous"], 8)
    self.assertEqual(
        len(decision_compiler.publish_blockers(list(self.meeting.decisions.all()))), 8)

def test_fake_mode_runs_the_whole_flow_without_any_llm(self):
    with patch.dict(os.environ, {}, clear=True):
        summary = decision_compiler.compile_meeting(self.meeting)
    self.assertEqual(summary["decisions"], 3)
    self.assertEqual(summary["ambiguous"], 2)
    self.assertEqual(summary["few_candidates"], 0)
    self.assertEqual(Interpretation.objects.filter(decision__meeting=self.meeting).count(), 4)
    self.assertEqual(Interpretation.objects.filter(decision__meeting=self.meeting, recommended=True).count(), 2)
    clear = self.meeting.decisions.filter(divergence_reason="")
    self.assertEqual(clear.count(), 1)
    self.assertTrue(clear.get().final_text)
```

- [ ] **Step 2: 실패를 확인한다**

Run: `cd APL_04 && python manage.py test meetalign.tests.DecisionCompileTests -v 2`
Expected: FAIL — `KeyError: 'ambiguous'`, `warned` 키를 돌려주는 옛 요약

- [ ] **Step 3: 구현한다**

`_compile_prompt(source)`를 스펙 §5.2의 요구대로 다시 쓴다 — 응답 스키마(`topic`/`ambiguous`/`why`/`candidates[text,evidence,recommended]`/`final`/`evidence`), "그대로 공개될 문장"일 것, 해설체·정답 단정 금지, `evidence`는 원문 복사, `recommended`는 하나만, 모호한 것이 많으면 상위 5개만.

`compile_specs(source)`: `services._enabled()`와 `source`가 모두 있을 때만 LLM을 부르고, 아니면 `parse_decisions(json.dumps(FAKE_DECISIONS))`를 돌려준다. `_generate_parsed(...)`는 그대로 쓰되 `soft_fail` 인자를 없앤다(관대 처리가 항목 단위로 내려갔으므로).

`compile_meeting(meeting)`: 항목마다
1. 명확 항목이면 `safety_warnings(final, evidence)`를 부른다. 사유가 있으면 `warnings`에 넣고, `final` 문장을 `Interpretation`(`order=0`, `evidence`, `recommended=False`) 하나로 저장하고 `final_text`는 비운다. 사유가 없으면 `final_text = final`.
2. 모호 항목이면 `divergence_reason = why`, 후보를 순서대로 `Interpretation`으로 저장, `final_text`는 빈다. 규칙은 부르지 않는다.
3. `parse_decisions`가 근거 검증으로 `final`을 비운 명확 항목은 모호로 올린다 — `divergence_reason`에 `"근거를 원문에서 확인하지 못했어요"`를 넣고 후보 0개로 둔다.
4. `Decision`은 `what=topic`, `who`/`due_text`/`done_criteria`는 `""`, `confirm_state`는 기본값으로 만든다.
요약은 `{"decisions", "ambiguous", "few_candidates"}` — `few_candidates`는 확인 필요인데 후보가 0개인 항목 수.

`FAKE_DECISIONS`를 새 스키마의 고정 응답으로 교체한다: 모호 2건(로그 보관 기간 / QA 범위, 각 후보 2개·`recommended` 하나·`evidence` 빈 문자열) + 명확 1건(배포 스크립트 정리). `DecisionConfirmTests.setUp`의 `self.warned`/`self.plain` 분류를 `d.warnings` → `d.has_warning`으로 바꾼다(가짜 모드의 모호 2건은 `divergence_reason`만 갖고 `warnings`는 비기 때문이다). 두 개수(2와 1)는 그대로 유지된다. `_interpretation_prompt`·`interpretations_for`·`_fake_interpretations`와 `MIN_INTERPRETATIONS`·`MAX_WARNED_DECISIONS`를 삭제한다. 모듈 docstring의 ①② 설명을 새 흐름으로 고친다.

- [ ] **Step 4: 통과를 확인한다**

Run: `cd APL_04 && python manage.py test meetalign -v 1`
Expected: `Ran 327 tests` — 이 시점에 `views._recompute_warnings`가 지워진 `warnings_for`를 부르고 있으면 그 호출만 임시로 제거해 전체를 녹색으로 만든 뒤 Task 6에서 화면을 정리한다.

- [ ] **Step 5: 커밋**

```bash
git add APL_04/meetalign/decision_compiler.py APL_04/meetalign/views.py APL_04/meetalign/tests.py
git commit -m "컴파일: AI 호출을 한 번으로 합치고 규칙 적발은 후보 1개로 승격한다"
```

**Task 5 종료 시 테스트 수: 327** (321 − 삭제 1 + 신규 7)

---

### Task 6: 진행자 화면 — 입력 칸 제거와 두 묶음

**Files:**
- Modify: `APL_04/meetalign/views.py:390-427` (`_save_decision_edits` 축소), `:339-353` (`_recompute_warnings` 삭제), `:485-498` (`_current_choice`), `:502-607` (`decision_review` 컨텍스트)
- Modify: `APL_04/meetalign/templates/meetalign/decision_review.html`
- Test: `APL_04/meetalign/tests.py` (`DecisionReviewTests`)

**Interfaces:**
- Consumes: Task 5의 요약 키(`ambiguous`), Task 1의 `has_warning`
- Produces: `decision_review` 컨텍스트에 `ambiguous_rows: list`, `clear_rows: list`, `ambiguous_count: int` (기존 `warned_count`를 대체). `rows`는 유지한다.

- [ ] **Step 1: 실패하는 테스트를 쓴다** — 명세 수정 관련 3개를 지운다

```python
# 삭제: test_editing_a_spec_clears_the_warnings_it_fixes,
#       test_saving_a_blank_what_leaves_the_item_untouched,
#       test_dropping_every_candidate_leaves_the_item_with_none

def test_the_screen_has_no_spec_input_fields_left(self):
    self._compile()
    html = self.client.get(self.url).content.decode()
    decision = self.meeting.decisions.first()
    for name in ("who_", "due_", "done_", "confirm_state_", "drop_interpretation_"):
        self.assertNotIn("%s%d" % (name, decision.id), html)
    self.assertNotIn('name="interpretation_', html)

def test_the_recommended_candidate_is_not_preselected(self):
    self._compile()
    r = self.client.get(self.url)
    self.assertContains(r, "AI 추천")
    for row in r.context["ambiguous_rows"]:
        for interpretation in row["interpretations"]:
            self.assertFalse(interpretation.chosen)
    self.assertNotIn("checked", r.content.decode())

def test_the_server_ignores_posted_spec_edits(self):
    self._compile()
    decision = self.meeting.decisions.first()
    before = decision.what
    self.client.post(self.url, {"action": "save",
                                "who_%d" % decision.id: "바뀐 담당자",
                                "what_%d" % decision.id: "바뀐 안건"})
    decision.refresh_from_db()
    self.assertEqual(decision.who, "")
    self.assertEqual(decision.what, before)
```

- [ ] **Step 2: 실패를 확인한다**

Run: `cd APL_04 && python manage.py test meetalign.tests.DecisionReviewTests -v 2`
Expected: FAIL — `who_<id>` 입력이 HTML에 남아 있고 `ambiguous_rows`가 없다

- [ ] **Step 3: 뷰와 템플릿을 구현한다**

`views.py`:
- `_recompute_warnings` 삭제, `_save_decision_edits`에서 명세 수정 블록과 후보 수정·버리기 블록 삭제 → `_apply_host_choice`만 항목마다 부른다.
- `_current_choice`: 확인 필요가 아닌 항목(`not decision.has_warning`)이고 `marked_undecided`가 아니면 `("", decision.final_text)`를 돌려준다 — B 묶음의 접힌 '고치기'에서 아무것도 선택돼 있지 않고, '직접 쓰기' 칸에는 AI 문장이 채워져 고치기 쉽게 한다.
- `decision_review` 컨텍스트: `warned_count` → `ambiguous_count`, `rows`를 `ambiguous_rows`(`row["decision"].has_warning`)와 `clear_rows`로 나눠 함께 넣는다. `max_warnings`·`min_interpretations` 키를 지운다.

`decision_review.html`:
- 입력 칸 4개·확정 여부 라디오·후보 수정 input·버리기 체크박스를 지운다.
- A 묶음: 제목 "확인이 필요한 결정 N개", 카드마다 `divergence_reason`을 굵게, `warnings`를 `record` 블록에 작게, 후보 라디오(문장 + `근거: …` + `recommended`면 `badge processing`으로 "AI 추천"), 그다음 '직접 쓰기'·'다음 회의로 넘김'.
- B 묶음: 제목 "그대로 공개될 결정 M개", `final_text`를 보여주고 `<details><summary>고치기</summary>`에 '직접 쓰기'·'다음 회의로 넘김' 라디오(빼기는 Task 7).
- `{% block note %}`의 1·2번 줄과 배지·안내 문구에서 "경고"를 "확인 필요"로 바꾼다. `spec_line` 출력(67줄)은 지운다.

- [ ] **Step 4: 통과를 확인한다**

Run: `cd APL_04 && python manage.py test meetalign -v 1`
Expected: `Ran 327 tests`, FAILED 없음

- [ ] **Step 5: 커밋**

```bash
git add APL_04/meetalign/views.py APL_04/meetalign/templates/meetalign/decision_review.html APL_04/meetalign/tests.py
git commit -m "진행자 화면: 명세 입력 칸을 없애고 확인 필요·그대로 공개 두 묶음으로 나눈다"
```

**Task 6 종료 시 테스트 수: 327** (327 − 삭제 3 + 신규 3)

---

### Task 7: '이 결정 빼기'

**Files:**
- Modify: `APL_04/meetalign/decision_compiler.py` (`CHOICE_DROP` 추가)
- Modify: `APL_04/meetalign/views.py:356-387` (`_apply_host_choice`)
- Modify: `APL_04/meetalign/templates/meetalign/decision_review.html`
- Test: `APL_04/meetalign/tests.py` (`DecisionConfirmTests`)

**Interfaces:**
- Consumes: Task 6의 `ambiguous_rows`/`clear_rows`, 기존 `_apply_host_choice`
- Produces: `decision_compiler.CHOICE_DROP = "drop"`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
def test_a_clear_item_can_be_rewritten_by_hand(self):
    decision = self.plain[0]          # 확인 필요가 아닌 항목 (setUp이 1개 만든다)
    self.client.post(self.url, {"action": "save",
                                "choice_%d" % decision.id: decision_compiler.CHOICE_CUSTOM,
                                "custom_text_%d" % decision.id: "민수가 금요일 18시까지 끝낸다"})
    decision.refresh_from_db()
    self.assertEqual(decision.final_text, "민수가 금요일 18시까지 끝낸다")

def test_dropping_a_clear_item_removes_it_from_the_publish_set(self):
    decision = self.plain[0]
    self.client.post(self.url, {"action": "save",
                                "choice_%d" % decision.id: decision_compiler.CHOICE_DROP})
    self.assertFalse(Decision.objects.filter(pk=decision.pk).exists())
    self.assertEqual(Interpretation.objects.filter(decision_id=decision.pk).count(), 0)
    self.assertEqual(self.client.get(self.url).context["draft_count"],
                     self.meeting.decisions.count())

def test_dropping_an_ambiguous_item_works_the_same_way(self):
    decision = self.warned[0]
    self.client.post(self.url, {"action": "save",
                                "choice_%d" % decision.id: decision_compiler.CHOICE_DROP})
    self.assertFalse(Decision.objects.filter(pk=decision.pk).exists())

def test_dropping_is_refused_after_publishing(self):
    decision = self.plain[0]
    Decision.objects.filter(pk=decision.pk).update(status=Decision.STATUS_PUBLISHED)
    r = self.client.post(self.url, {"action": "save",
                                    "choice_%d" % decision.id: decision_compiler.CHOICE_DROP})
    self.assertTrue(Decision.objects.filter(pk=decision.pk).exists())
    self.assertContains(r, "공개된 항목은 뺄 수 없습니다")

def test_dropping_every_item_leaves_nothing_to_publish(self):
    for decision in list(self.meeting.decisions.all()):
        self.client.post(self.url, {"action": "save",
                                    "choice_%d" % decision.id: decision_compiler.CHOICE_DROP})
    self.assertEqual(self.meeting.decisions.count(), 0)
    r = self.client.post(self.url, {"action": "publish"})
    self.assertContains(r, "공개할 항목이 없습니다")
```

- [ ] **Step 2: 실패를 확인한다**

Run: `cd APL_04 && python manage.py test meetalign.tests.DecisionConfirmTests -v 2`
Expected: FAIL — `AttributeError: ... CHOICE_DROP`

- [ ] **Step 3: 구현한다**

`decision_compiler.CHOICE_DROP = "drop"`.
`views._apply_host_choice`에 분기를 더한다 — `raw == CHOICE_DROP`이면 `decision.status != Decision.STATUS_DRAFT`일 때 `"공개된 항목은 뺄 수 없습니다. 내용을 고치려면 '확정 문장 수정'을 쓰세요."`를 돌려주고, draft면 `decision.delete()` 후 `None`을 돌려준다(삭제했으므로 뒤이은 `save(update_fields=...)`를 타지 않게 즉시 반환한다). `_save_decision_edits`는 항목 목록을 돌며 호출하므로, 삭제된 항목을 다시 만지지 않도록 반복 중 수집한 객체만 쓴다.

`decision_review.html`: A 묶음 라디오 맨 아래에 `이 결정 빼기 — 회의에 없던 결정이에요`를 기존 `meta` 스타일로 붙이고, B 묶음의 `<details>` 안 라디오에도 셋째로 붙인다.

- [ ] **Step 4: 통과를 확인한다**

Run: `cd APL_04 && python manage.py test meetalign -v 1`
Expected: `Ran 331 tests`, FAILED 없음

- [ ] **Step 5: 커밋**

```bash
git add APL_04/meetalign/decision_compiler.py APL_04/meetalign/views.py APL_04/meetalign/templates/meetalign/decision_review.html APL_04/meetalign/tests.py
git commit -m "진행자 확정: 회의에 없던 결정을 빼는 선택지를 두 묶음에 모두 추가"
```

**Task 7 종료 시 테스트 수: 331** (327 + 신규 4)

---

## 테스트 수 흐름

| Task | 삭제 | 신규 | 종료 시 |
|---|---|---|---|
| 시작 | | | 325 |
| 1 모델·0022 | 2 | 1 | 324 |
| 2 미정 자동 문장 | 0 | 1 | 325 |
| 3 통합 파서 | 1 | 1 | 325 |
| 4 규칙 안전망 | 7 | 3 | 321 |
| 5 compile_meeting | 1 | 7 | 327 |
| 6 진행자 화면 | 3 | 3 | 327 |
| 7 이 결정 빼기 | 0 | 4 | 331 |
| 합계 | 14 | 20 | **331** |

재작성(수량 불변)은 Task 3의 파서·근거 검증 9개, Task 4의 규칙 5개, Task 5의 `fake_mode`·`compile_uses_the_llm_response` 2개, Task 1의 `is_settled` 1개로 모두 17개다. 스펙 §7이 적은 "재작성 12개"는 `published_text` 1건이 삭제 목록과 겹쳐 잘못 센 것이고, 재작성은 수량에 영향이 없으므로 총계 331은 그대로다.

표의 숫자는 **테스트 함수 수**다. 전체 스위트가 녹색이어야 하는 지점은 Task 1·2·5·6·7의 Step 4이고, Task 3과 4는 `compile_meeting`이 아직 옛 스키마를 쓰는 중간 상태이므로 해당 테스트 클래스만 녹색으로 두고 커밋한다 — Task 5 Step 4에서 전체가 다시 녹색이 된다. 이 두 커밋은 의도된 중간 상태이며, 브랜치를 머지하기 전에는 Task 7까지 끝나 있어야 한다.
