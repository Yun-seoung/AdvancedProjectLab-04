# 확정 문장 후보 재설계 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** AI가 모호한 결정을 찾아 그대로 공개될 확정 문장 후보 2~3개를 만들고, 진행자가 읽고 탭 한 번으로 고르는 흐름으로 결정 컴파일러의 확정 단계를 바꾼다.

**Architecture:** 전사 원문을 LLM에 한 번 보내 결정·모호 여부·이유·후보·근거·추천을 한 응답으로 받는다. 규칙은 안전망으로 축소해 명확 판정 항목의 확정 문장과 원문 근거만 스캔한다. 공개되는 문장은 모든 상태에서 `Decision.final_text` 하나로 통일한다. 진행자 화면의 명세 입력 칸을 없애고 라디오 선택만 남긴다.

**Tech Stack:** Django 5 (SQLite), `django.test.TestCase`, LLM 호출은 `services._generate_clean`/`_enabled`/`_normalize_for_match` 재사용, 템플릿은 Django 템플릿 + 기존 디자인 토큰.

**Spec:** `docs/superpowers/specs/2026-11-04-decision-candidates-design.md`

## Global Constraints

- **모든 커밋에서 전체 테스트가 녹색이어야 한다** (PR #40·#41과 같은 기준). 그래서 Task 3·4는 새 함수를 옛 함수 **옆에 추가만** 하고, 옛 함수 삭제와 `compile_meeting` 호출 교체는 Task 5에서 한 번에 한다. 각 Task의 Step 4는 항상 전체 스위트를 돌린다.
- `APL_04/meetalign/services.py`는 수정하지 않는다. `_generate_clean(prompt)`, `_normalize_for_match(text)`, `_enabled()`만 호출해 쓴다.
- 새 CSS 프레임워크·CDN 금지. 기존 토큰(`card elevated`, `badge`, `record`, `meta`, `label`, `success`, `notice`, `item`, `plain`)만 쓴다.
- 톤: "틀렸다"가 아니라 "다르게 이해할 수 있다". 화면 문구는 "확인 필요"를 쓰고 "경고"를 쓰지 않는다(모델 필드명 `warnings`는 유지).
- 마이그레이션은 `0022` 하나만 추가한다. 기존 필드를 `RemoveField`로 지우지 않는다.
- 상수: `MAX_DECISIONS = 12`, `MAX_INTERPRETATIONS = 3`, `PARSE_RETRIES = 1`. `MIN_INTERPRETATIONS`·`MAX_WARNED_DECISIONS`는 Task 5에서 삭제한다.
- 새 파서의 이름은 `parse_items`다 — 옛 `parse_decisions`가 Task 5까지 살아 있어야 해서 이름이 겹칠 수 없고, Task 5에서 옛 함수를 지운 뒤에도 되돌리지 않는다(이름 변경이 Task 5의 diff를 흐리기 때문).
- 테스트 명령은 `APL_04`에서 `python manage.py test meetalign`. 각 Task 끝에 `Ran N tests`와 `OK`를 확인한다.
- `git add`는 파일을 지정해서 한다. `push`는 하지 않는다. 커밋 메시지는 한국어로 쓰고 끝에 `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`를 붙인다.

## Review Focus

스펙이 전제하지만 어느 Task의 주 테스트도 건드리지 않는 입력들이다. 다섯 건 모두 **새 테스트 함수가 아니라 소유 Task의 신규 테스트 안 추가 assertion**으로 고정한다(총계 331을 지키기 위함).

1. `candidates`가 객체 배열이 아니라 문자열 배열(`["문장1", "문장2"]`)로 온다 → 그 후보들을 조용히 버리고 예외를 올리지 않는다. (Task 3 / `test_parse_items_reads_the_new_schema`)
2. `recommended`가 `"true"`·`1` 같은 비불리언으로 온다 → 참으로 보고 배지를 붙인다. (Task 3 / `test_parse_items_keeps_only_one_recommendation`)
3. `ambiguous:true`인데 `final`도 함께 온다 → `candidates`를 쓰고 `final`은 무시한다. (Task 3 / `test_parse_items_reads_the_new_schema`)
4. `topic`이 200자를 넘게 온다 → 저장 시 120자로 자른다(카드 제목이 레이아웃을 깨지 않게). (Task 3 / `test_parse_items_drops_items_without_a_topic`)
5. 진행자가 모든 항목을 '이 결정 빼기'로 지워 draft가 0개가 된다 → 공개 버튼이 막히고 기존 "공개할 항목이 없습니다" 사유가 나온다. (Task 7 / `test_dropping_every_item_leaves_nothing_to_publish`)

---

### Task 1: 마이그레이션 0022와 새 모델 칸

옛 흐름을 그대로 둔 채 칸만 늘린다. `spec_line`·`published_text` 폴백·템플릿은 Task 5까지 손대지 않는다 — 옛 `interpretations_for`가 `spec_line`을 쓰고, 옛 테스트가 폴백 문장을 본다.

**Files:**
- Create: `APL_04/meetalign/migrations/0022_decision_candidates.py`
- Modify: `APL_04/meetalign/models.py:136-245` (Decision, Interpretation)
- Modify: `APL_04/meetalign/admin.py:16-20`
- Test: `APL_04/meetalign/tests.py` (`DecisionModelTests`)

**Interfaces:**
- Consumes: 없음 (첫 Task)
- Produces: `Decision.divergence_reason: str`, `Interpretation.recommended: bool`,
  `Decision.has_warning -> bool` (= `bool(warnings or divergence_reason)`),
  `migrations.0022_decision_candidates.spec_sentence(who: str, what: str, due: str, done: str) -> str`,
  `migrations.0022_decision_candidates.fill_missing_final_text(Decision) -> int`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
def _migration(self):
    from importlib import import_module
    return import_module("meetalign.migrations.0022_decision_candidates")

def test_0022_builds_a_sentence_from_the_old_fields(self):
    spec_sentence = self._migration().spec_sentence
    self.assertEqual(spec_sentence("민수", "배포", "금요일", "확인"), "민수 · 배포 · 금요일 · 확인")
    self.assertEqual(spec_sentence("", "배포", "", ""), "배포")
    self.assertEqual(spec_sentence("", "", "", ""), "")

def test_0022_backfills_only_published_rows_without_a_sentence(self):
    fill = self._migration().fill_missing_final_text
    draft = Decision.objects.create(meeting=self.meeting, order=11, who="민수", what="배포",
                                    due_text="금요일", done_criteria="확인")
    empty = Decision.objects.create(meeting=self.meeting, order=12, who="민수", what="문서",
                                    status=Decision.STATUS_PUBLISHED)
    kept = Decision.objects.create(meeting=self.meeting, order=13, what="QA",
                                   status=Decision.STATUS_UNDECIDED, final_text="이미 있는 문장")
    self.assertEqual(fill(Decision), 1)
    draft.refresh_from_db(); empty.refresh_from_db(); kept.refresh_from_db()
    self.assertEqual(draft.final_text, "")                    # draft는 건드리지 않는다
    self.assertEqual(empty.final_text, "민수 · 문서")          # 빈 공개 항목만 채운다
    self.assertEqual(kept.final_text, "이미 있는 문장")        # 있는 문장은 덮지 않는다

# 재작성(수량 불변): test_is_settled_requires_a_sentence_or_the_undecided_mark 에 아래를 더한다
#   divergence_reason 만 있는 항목은 is_settled가 False, has_warning이 True
```

- [ ] **Step 2: 실패를 확인한다**

Run: `cd APL_04 && python manage.py test meetalign.tests.DecisionModelTests -v 2`
Expected: FAIL — `ModuleNotFoundError: meetalign.migrations.0022_decision_candidates`

- [ ] **Step 3: 모델과 마이그레이션을 구현한다**

`models.py`:
- `Decision.divergence_reason = models.TextField(blank=True, default="")`
- `Interpretation.recommended = models.BooleanField(default=False)`
- `Decision.has_warning` → `bool(self.warnings or self.divergence_reason)`
- `Decision.is_settled` → `bool(self.final_text) or self.marked_undecided or not self.has_warning`

`admin.py`: `list_display`·`search_fields`의 `who` → `final_text`.

`migrations/0022_decision_candidates.py`: `AddField` 2개 + `RunPython(_forward, migrations.RunPython.noop)`.
- `spec_sentence(who, what, due, done)` — 빈칸을 건너뛰고 `" · "`로 잇는다.
- `fill_missing_final_text(Decision)` — `status`가 `published`/`undecided`이고 `final_text`가 `""`인 레코드만 `spec_sentence(...) or what`으로 채우고 채운 수를 돌려준다.
- `_forward(apps, schema_editor)`는 `apps.get_model("meetalign", "Decision")`을 `fill_missing_final_text`에 넘긴다.

- [ ] **Step 4: 전체 테스트가 통과하는지 확인한다**

Run: `cd APL_04 && python manage.py makemigrations --check --dry-run meetalign && python manage.py test meetalign -v 1`
Expected: 마이그레이션 누락 없음, `Ran 327 tests` + `OK`

- [ ] **Step 5: 커밋**

```bash
git add APL_04/meetalign/models.py APL_04/meetalign/admin.py APL_04/meetalign/migrations/0022_decision_candidates.py APL_04/meetalign/tests.py
git commit -m "모델: divergence_reason·recommended 칸 추가와 공개 문장 보정 (마이그레이션 0022)"
```

**Task 1 종료 시 테스트 수: 327** (325 + 신규 2)

---

### Task 2: 미정 항목의 자동 문장

`published_text`의 폴백을 아직 지우지 않으므로 템플릿은 건드리지 않는다. 이 Task는 '다음 회의로 넘김'이 문장을 **갖게** 만드는 것까지다.

**Files:**
- Modify: `APL_04/meetalign/decision_compiler.py` (`undecided_sentence` 추가)
- Modify: `APL_04/meetalign/views.py:366-368` (`_apply_host_choice`의 undecided 분기)
- Test: `APL_04/meetalign/tests.py` (`DecisionConfirmTests`)

**Interfaces:**
- Consumes: 없음
- Produces: `decision_compiler.undecided_sentence(topic: str) -> str`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
def test_marking_undecided_writes_a_sentence_of_its_own(self):
    decision = self.warned[0]
    self.client.post(self.url, {"action": "save",
                                "choice_%d" % decision.id: decision_compiler.CHOICE_UNDECIDED})
    decision.refresh_from_db()
    self.assertTrue(decision.marked_undecided)
    self.assertEqual(decision.final_text,
                     "%s — 이번 회의에서 결론이 나지 않았음 (다음 회의로)" % decision.what)
    self.assertEqual(decision.published_text, decision.final_text)

# 재작성(수량 불변): test_marking_undecided_sets_the_flag_and_clears_the_sentence
#   → 이름을 test_marking_undecided_sets_the_flag_and_writes_a_sentence 로 바꾸고
#     final_text == "" 단정을 위 문장 단정으로 교체한다.
```

- [ ] **Step 2: 실패를 확인한다**

Run: `cd APL_04 && python manage.py test meetalign.tests.DecisionConfirmTests -v 2`
Expected: FAIL — `final_text`가 `""`

- [ ] **Step 3: 구현한다**

`decision_compiler.undecided_sentence(topic)` → `"%s — 이번 회의에서 결론이 나지 않았음 (다음 회의로)" % topic`.
`views._apply_host_choice`의 `CHOICE_UNDECIDED` 분기에서 `decision.final_text = decision_compiler.undecided_sentence(decision.what)`로 채운다(기존 `= ""`를 교체).

- [ ] **Step 4: 전체 테스트가 통과하는지 확인한다**

Run: `cd APL_04 && python manage.py test meetalign -v 1`
Expected: `Ran 328 tests` + `OK`

- [ ] **Step 5: 커밋**

```bash
git add APL_04/meetalign/decision_compiler.py APL_04/meetalign/views.py APL_04/meetalign/tests.py
git commit -m "미정 항목도 자기 문장을 갖는다 — '다음 회의로 넘김'에 확정 문장 자동 생성"
```

**Task 2 종료 시 테스트 수: 328** (327 + 신규 1)

---

### Task 3: 통합 응답 파서 추가

옛 `parse_decisions`·`parse_interpretations`를 그대로 두고 `parse_items`를 **추가**한다. 아직 아무도 부르지 않는다.

**Files:**
- Modify: `APL_04/meetalign/decision_compiler.py` (`parse_items` 추가)
- Test: `APL_04/meetalign/tests.py` (`DecisionParseTests`)

**Interfaces:**
- Consumes: 없음 (순수 함수)
- Produces: `decision_compiler.parse_items(raw: str, source_text: str | None = None) -> list[dict]`,
  항목 모양 `{"topic": str, "ambiguous": bool, "why": str, "final": str, "evidence": str, "candidates": [{"text": str, "evidence": str, "recommended": bool}]}`

- [ ] **Step 1: 실패하는 테스트 11개를 쓴다**

```python
TOPIC = "로그 보관 기간"
CANDIDATES = [{"text": "민수가 30일로 바꾼다", "evidence": "", "recommended": True},
              {"text": "민수가 90일로 바꾼다", "evidence": ""}]

def test_parse_items_reads_the_new_schema(self):
    raw = json.dumps([
        {"topic": TOPIC, "ambiguous": True, "why": "30일인지 90일인지 갈립니다",
         "final": "무시되어야 한다", "candidates": CANDIDATES},
        {"topic": "후보가 문자열", "ambiguous": True, "why": "x",
         "candidates": ["문장만 왔다", "이것도"]},
        {"topic": "배포", "ambiguous": False, "final": "민수가 금요일까지 배포한다",
         "evidence": "민수가 금요일까지 배포를 맡기로 했습니다."},
    ], ensure_ascii=False)
    items = decision_compiler.parse_items(raw)
    self.assertEqual(items[0]["why"], "30일인지 90일인지 갈립니다")
    self.assertEqual([c["text"] for c in items[0]["candidates"]],
                     ["민수가 30일로 바꾼다", "민수가 90일로 바꾼다"])
    self.assertEqual(items[0]["final"], "")       # ambiguous면 final은 버린다
    self.assertEqual(items[1]["candidates"], [])  # 문자열 후보는 조용히 버린다
    self.assertEqual(items[2]["candidates"], [])
    self.assertEqual(items[2]["final"], "민수가 금요일까지 배포한다")

def test_parse_items_keeps_only_one_recommendation(self):
    raw = json.dumps([{"topic": TOPIC, "ambiguous": True, "why": "x", "candidates": [
        {"text": "첫째", "recommended": "true"}, {"text": "둘째", "recommended": True}]}],
        ensure_ascii=False)
    self.assertEqual([c["recommended"] for c in decision_compiler.parse_items(raw)[0]["candidates"]],
                     [True, False])

def test_parse_items_drops_items_without_a_topic(self):
    raw = json.dumps([{"topic": "", "ambiguous": False, "final": "문장"},
                      {"topic": "가" * 300, "ambiguous": False, "final": "문장"}], ensure_ascii=False)
    items = decision_compiler.parse_items(raw)
    self.assertEqual(len(items), 1)
    self.assertEqual(len(items[0]["topic"]), 120)

def test_parse_items_survives_code_fences_and_prose(self):
    raw = "네.\n```json\n[{\"topic\": \"배포\", \"ambiguous\": false, \"final\": \"문장\"}]\n```\n이상."
    self.assertEqual(len(decision_compiler.parse_items(raw)), 1)

def test_parse_items_returns_empty_on_broken_json(self):
    for raw in ("JSON 못 만들겠습니다", '[{"topic": "배포",]', '{"topic": "배포"}'):
        self.assertEqual(decision_compiler.parse_items(raw), [])

def test_parse_items_caps_the_number_of_items(self):
    raw = json.dumps([{"topic": "안건 %d" % i, "ambiguous": False, "final": "문장"}
                      for i in range(30)], ensure_ascii=False)
    self.assertEqual(len(decision_compiler.parse_items(raw)), decision_compiler.MAX_DECISIONS)

def test_parse_items_drops_duplicate_candidates(self):
    raw = json.dumps([{"topic": TOPIC, "ambiguous": True, "why": "x", "candidates": [
        {"text": ""}, {"text": "같은 후보"}, {"text": "같은 후보"}]}], ensure_ascii=False)
    self.assertEqual([c["text"] for c in decision_compiler.parse_items(raw)[0]["candidates"]],
                     ["같은 후보"])

def test_parse_items_caps_candidates(self):
    raw = json.dumps([{"topic": TOPIC, "ambiguous": True, "why": "x",
                       "candidates": [{"text": "후보 %d" % i} for i in range(8)]}], ensure_ascii=False)
    self.assertEqual(len(decision_compiler.parse_items(raw)[0]["candidates"]),
                     decision_compiler.MAX_INTERPRETATIONS)

def test_parse_items_drops_a_candidate_whose_evidence_is_not_in_the_transcript(self):
    source = "민수가 금요일까지 배포를 맡기로 했습니다."
    raw = json.dumps([{"topic": TOPIC, "ambiguous": True, "why": "x", "candidates": [
        {"text": "남는다", "evidence": "민수가 금요일까지 배포를 맡기로 했습니다."},
        {"text": "버려진다", "evidence": "회의에 없던 문장입니다."}]}], ensure_ascii=False)
    items = decision_compiler.parse_items(raw, source_text=source)
    self.assertEqual([c["text"] for c in items[0]["candidates"]], ["남는다"])

def test_parse_items_matches_evidence_loosely_and_skips_blanks(self):
    source = "민수가 금요일까지 배포를 맡기로 했습니다."
    raw = json.dumps([{"topic": TOPIC, "ambiguous": True, "why": "x", "candidates": [
        {"text": "공백 무시", "evidence": "민수가  금요일까지\n배포를 맡기로"},
        {"text": "근거 없음", "evidence": ""}]}], ensure_ascii=False)
    items = decision_compiler.parse_items(raw, source_text=source)
    self.assertEqual(len(items[0]["candidates"]), 2)

def test_parse_items_clears_a_final_whose_evidence_is_not_in_the_transcript(self):
    raw = json.dumps([{"topic": TOPIC, "ambiguous": False, "final": "민수가 30일로 바꾼다",
                       "evidence": "회의에 없던 문장"}], ensure_ascii=False)
    self.assertEqual(decision_compiler.parse_items(raw, source_text="로그 이야기")[0]["final"], "")
```

- [ ] **Step 2: 실패를 확인한다**

Run: `cd APL_04 && python manage.py test meetalign.tests.DecisionParseTests -v 2`
Expected: FAIL — `AttributeError: ... has no attribute 'parse_items'`

- [ ] **Step 3: `parse_items(raw, source_text=None)`를 구현한다**

`_json_array(raw)`를 그대로 쓴다. 항목마다: `topic`이 비면 버리고 120자로 자른다. `ambiguous`는 `bool(...)`. 참이면 `final`·`evidence`를 빈 문자열로 만들고 `candidates`를 읽고, 거짓이면 `candidates`를 비우고 `final`·`evidence`를 읽는다.
후보는 `dict`가 아니면 건너뛰고, `text`가 비면 건너뛰고, 같은 `text`는 한 번만 받고, `MAX_INTERPRETATIONS`에서 자른다. `recommended`는 `bool(...)`로 읽어 참인 것 중 첫 번째만 남기고 나머지는 `False`로 되돌린다.
`source_text`가 있으면 `services._normalize_for_match`로 근거를 검증한다 — 후보는 버리고, 명확 항목은 `final`을 빈 문자열로 만든다. `evidence`가 빈 문자열이면 검증을 건너뛴다. 항목 수는 `MAX_DECISIONS`에서 자른다.

- [ ] **Step 4: 전체 테스트가 통과하는지 확인한다**

Run: `cd APL_04 && python manage.py test meetalign -v 1`
Expected: `Ran 339 tests` + `OK` (옛 파서와 그 테스트는 그대로 살아 있다)

- [ ] **Step 5: 커밋**

```bash
git add APL_04/meetalign/decision_compiler.py APL_04/meetalign/tests.py
git commit -m "파서: 결정·모호 여부·이유·후보·근거·추천을 한 응답에서 읽는 parse_items 추가"
```

**Task 3 종료 시 테스트 수: 339** (328 + 신규 11)

---

### Task 4: 규칙 안전망 추가

옛 `warnings_for`·`_blank_warnings`·`_cross_warnings`를 그대로 두고 `safety_warnings`를 **추가**한다.

**Files:**
- Modify: `APL_04/meetalign/decision_compiler.py` (`safety_warnings` 추가)
- Test: `APL_04/meetalign/tests.py` (`DecisionWarningTests`)

**Interfaces:**
- Consumes: 없음 (순수 함수)
- Produces: `decision_compiler.safety_warnings(final_text: str, evidence: str) -> list[str]`

- [ ] **Step 1: 실패하는 테스트 6개를 쓴다**

```python
def test_safety_catches_an_ambiguous_phrase_in_the_sentence(self):
    self.assertEqual(decision_compiler.safety_warnings("나중에 로깅 기준을 맞춘다", "로깅은 맞춰야죠"),
                     ["확정 문장에 모호한 표현이 있어요: '나중에'"])

def test_safety_catches_an_ambiguous_phrase_only_in_the_transcript(self):
    # 원문은 모호한데 AI가 혼자 해소한 경우 — 진행자가 확인해야 한다
    self.assertEqual(decision_compiler.safety_warnings("민수가 다음 주 금요일까지 배포한다",
                                                       "다음 주쯤 할게요"),
                     ["원문에 모호한 표현이 있어요: '쯤'"])

def test_safety_lists_the_same_phrase_once(self):
    self.assertEqual(decision_compiler.safety_warnings("나중에 맞춘다", "나중에 맞추죠"),
                     ["확정 문장에 모호한 표현이 있어요: '나중에'"])

def test_safety_catches_conflicting_dates(self):
    self.assertEqual(decision_compiler.safety_warnings("금요일 아니면 월요일에 배포한다", ""),
                     ["확정 문장에 다른 날짜가 섞여 있어요: 금요일, 월요일"])

def test_safety_does_not_split_a_single_date(self):
    self.assertEqual(decision_compiler.safety_warnings("12월 5일까지 배포한다", ""), [])

def test_safety_catches_conflicting_quantities(self):
    self.assertEqual(decision_compiler.safety_warnings("테스트 3개를 쓰고 5개를 통과시킨다", ""),
                     ["확정 문장에 다른 숫자가 섞여 있어요: 3개, 5개"])
```

- [ ] **Step 2: 실패를 확인한다**

Run: `cd APL_04 && python manage.py test meetalign.tests.DecisionWarningTests -v 2`
Expected: FAIL — `AttributeError: ... has no attribute 'safety_warnings'`

- [ ] **Step 3: `safety_warnings(final_text, evidence)`를 구현한다**

`AMBIGUOUS_PHRASES`·`_DATE_EXPR`·`_QUANTITY_EXPR`·`_date_tokens`·`_quantity_tokens`를 그대로 쓴다. 한 텍스트에 대해 모호어와 날짜·숫자 충돌을 찾는 내부 함수를 하나 두고 `final_text`와 `evidence`에 각각 적용한다. 사유 문구는 네 가지다:

```
확정 문장에 모호한 표현이 있어요: '%s'
원문에 모호한 표현이 있어요: '%s'
확정 문장에 다른 날짜가 섞여 있어요: %s   /  확정 문장에 다른 숫자가 섞여 있어요: %s
원문에 다른 날짜가 섞여 있어요: %s      /  원문에 다른 숫자가 섞여 있어요: %s
```

토큰은 `sorted()` 후 `", "`로 잇는다. 순서는 확정 문장 사유 먼저, 원문 사유 다음. 같은 모호어가 양쪽에서 걸리면 확정 문장 쪽만 남긴다(중복 제거는 순서를 지키며 처음 것만).

- [ ] **Step 4: 전체 테스트가 통과하는지 확인한다**

Run: `cd APL_04 && python manage.py test meetalign -v 1`
Expected: `Ran 345 tests` + `OK`

- [ ] **Step 5: 커밋**

```bash
git add APL_04/meetalign/decision_compiler.py APL_04/meetalign/tests.py
git commit -m "규칙 안전망: 확정 문장과 원문 근거를 보는 safety_warnings 추가"
```

**Task 4 종료 시 테스트 수: 345** (339 + 신규 6)

---

### Task 5: 교체 — 호출 한 번, 승격, 옛 코드 제거

모든 커밋이 녹색이어야 하므로 교체와 삭제가 이 Task에 모인다. 한 커밋 안에서 `compile_meeting`을 새 파이프라인으로 바꾸고, 옛 함수·옛 테스트를 함께 지운다.

**Files:**
- Modify: `APL_04/meetalign/decision_compiler.py` (`_compile_prompt`, `compile_specs`, `compile_meeting`, `FAKE_DECISIONS`; `parse_decisions`·`parse_interpretations`·`warnings_for`·`_blank_warnings`·`_cross_warnings`·`_interpretation_prompt`·`interpretations_for`·`_fake_interpretations`·`MIN_INTERPRETATIONS`·`MAX_WARNED_DECISIONS` 삭제)
- Modify: `APL_04/meetalign/models.py` (`spec_line` 삭제, `published_text` 폴백 삭제)
- Modify: `APL_04/meetalign/views.py:339-353` (`_recompute_warnings` 삭제와 그 호출 제거)
- Modify: `APL_04/meetalign/templates/meetalign/decision_ack.html:26-27`, `meeting_detail.html:39`
- Test: `APL_04/meetalign/tests.py` (`DecisionCompileTests`, 옛 `DecisionParseTests`·`DecisionWarningTests`·`DecisionModelTests` 정리)

**Interfaces:**
- Consumes: Task 3의 `parse_items`, Task 4의 `safety_warnings`, Task 1의 모델 칸
- Produces: `compile_meeting(meeting) -> {"decisions": int, "ambiguous": int, "few_candidates": int}`,
  `compile_specs(source: str) -> list[dict]` (`parse_items` 항목 목록)

- [ ] **Step 1: 실패하는 테스트 9개를 쓰고 옛 테스트 27개를 지운다**

삭제 — `DecisionParseTests`의 옛 12개(`parse_decisions_*` 7개 + `interpretation*`·`evidence*`·`blank_evidence*` 5개), `DecisionWarningTests`의 옛 12개(전부), `DecisionModelTests`의 `spec_line_shows_blank_fields_as_question_marks`·`published_text_falls_back_to_the_spec_line`, `DecisionCompileTests`의 `dropped_warning_count_is_reported`.

재작성(수량 불변) — `fake_mode_runs_the_whole_flow_without_any_llm`, `compile_uses_the_llm_response_when_a_model_is_configured`, `items_without_warnings_get_no_interpretations`, `interpretations_with_fabricated_evidence_leave_the_item_without_candidates`, `transcript_is_sent_in_the_compile_prompt`, `DecisionConfirmTests.setUp`의 `self.warned`/`self.plain` 분류를 `d.warnings` → `d.has_warning`로.

```python
#   setUp의 transcript를 아래로 바꾼다 — 모든 테스트가 이 원문 안에서 근거를 찾는다.
SOURCE = ("로그는 일단 한 달 정도면 충분할 것 같은데요. "
          "배포 스크립트는 제가 금요일까지 정리할게요. "
          "QA는 다음 주쯤 할게요.")
CLEAR = {"topic": "배포 스크립트 정리", "ambiguous": False,
         "final": "민수가 금요일까지 배포 스크립트를 정리한다",
         "evidence": "배포 스크립트는 제가 금요일까지 정리할게요."}
AMBIGUOUS = {"topic": "로그 보관 기간", "ambiguous": True, "why": "30일인지 90일인지 갈립니다",
             "candidates": [{"text": "민수가 30일로 바꾼다",
                             "evidence": "로그는 일단 한 달 정도면 충분할 것 같은데요.",
                             "recommended": True},
                            {"text": "민수가 90일로 바꾼다",
                             "evidence": "로그는 일단 한 달 정도면 충분할 것 같은데요."}]}

def _compiled(self, *items):
    raw = json.dumps(list(items), ensure_ascii=False)
    with patch.dict(os.environ, {"OLLAMA_MODEL": "m1"}), \
         patch.object(services, "_generate", return_value=raw) as gen:
        summary = decision_compiler.compile_meeting(self.meeting)
    return summary, {d.what: d for d in self.meeting.decisions.all()}, gen

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
    _, by_topic, _ = self._compiled(dict(self.CLEAR, final="나중에 배포 스크립트를 정리한다"))
    decision = by_topic["배포 스크립트 정리"]
    self.assertEqual(decision.warnings, ["확정 문장에 모호한 표현이 있어요: '나중에'"])
    self.assertEqual([i.text for i in decision.interpretations.all()],
                     ["나중에 배포 스크립트를 정리한다"])
    self.assertEqual(decision.final_text, "")
    self.assertFalse(decision.is_settled)

def test_a_rule_hit_on_the_transcript_promotes_a_clear_item(self):
    _, by_topic, _ = self._compiled({"topic": "QA 범위", "ambiguous": False,
                                     "final": "민수가 다음 주 금요일까지 QA를 끝낸다",
                                     "evidence": "QA는 다음 주쯤 할게요."})
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
    _, by_topic, _ = self._compiled(dict(self.CLEAR, evidence="회의에 없던 문장입니다."))
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
    summary, _, _ = self._compiled(*[dict(self.AMBIGUOUS, topic="안건 %d" % i) for i in range(8)])
    self.assertEqual(summary["ambiguous"], 8)
    self.assertEqual(len(decision_compiler.publish_blockers(
        list(self.meeting.decisions.all()))), 8)
```

- [ ] **Step 2: 실패를 확인한다**

Run: `cd APL_04 && python manage.py test meetalign.tests.DecisionCompileTests -v 2`
Expected: FAIL — `KeyError: 'ambiguous'` (옛 요약은 `warned`를 돌려준다)

- [ ] **Step 3: 구현한다**

`_compile_prompt(source)`를 스펙 §5.2대로 다시 쓴다 — 응답 스키마, "그대로 공개될 문장"일 것, 해설체·정답 단정 금지, `evidence`는 원문 복사, `recommended`는 하나만, 모호한 것이 많으면 상위 5개만.

`compile_specs(source)`: `services._enabled()`와 `source`가 모두 있을 때만 LLM을 부르고, 아니면 `parse_items(json.dumps(FAKE_DECISIONS))`를 돌려준다. `_generate_parsed(...)`에서 `soft_fail` 인자를 없앤다(관대 처리가 항목 단위로 내려갔다).

`compile_meeting(meeting)`: 항목마다
1. 명확 항목이면 `safety_warnings(final, evidence)`를 부른다. 사유가 있으면 `warnings`에 넣고 `final` 문장을 `Interpretation`(`order=0`, 그 `evidence`, `recommended=False`) 하나로 저장하고 `final_text`는 비운다. 사유가 없으면 `final_text = final`.
2. 모호 항목이면 `divergence_reason = why`, 후보를 순서대로 `Interpretation`으로 저장, `final_text`는 빈다. 규칙은 부르지 않는다.
3. `parse_items`가 근거 검증으로 `final`을 비운 명확 항목은 `divergence_reason = "근거를 원문에서 확인하지 못했어요"`로 올리고 후보 0개로 둔다.
4. `Decision`은 `what=topic`, `who`·`due_text`·`done_criteria`는 `""`, `confirm_state`는 기본값.
요약은 `{"decisions", "ambiguous", "few_candidates"}` — `few_candidates`는 확인 필요인데 후보가 0개인 항목 수.

`FAKE_DECISIONS`를 새 스키마의 고정 응답으로 교체한다: 모호 2건(로그 보관 기간 / QA 범위, 각 후보 2개·`recommended` 하나·`evidence` 빈 문자열) + 명확 1건(배포 스크립트 정리).

옛 코드 제거 — `parse_decisions`, `parse_interpretations`, `warnings_for`, `_blank_warnings`, `_cross_warnings`, `_interpretation_prompt`, `interpretations_for`, `_fake_interpretations`, `MIN_INTERPRETATIONS`, `MAX_WARNED_DECISIONS`, `views._recompute_warnings`와 그 호출, `Decision.spec_line`, `published_text`의 폴백. 모듈 docstring의 ①② 설명을 새 흐름으로 고친다.

템플릿 — `decision_ack.html:26-27`의 분기를 `{{ row.decision.published_text }}` 하나로, `meeting_detail.html:39`의 `spec_line`을 `final_text`로.

- [ ] **Step 4: 전체 테스트가 통과하는지 확인한다**

Run: `cd APL_04 && python manage.py test meetalign -v 1`
Expected: `Ran 327 tests` + `OK`

- [ ] **Step 5: 커밋**

```bash
git add APL_04/meetalign/decision_compiler.py APL_04/meetalign/models.py APL_04/meetalign/views.py APL_04/meetalign/templates/meetalign/decision_ack.html APL_04/meetalign/templates/meetalign/meeting_detail.html APL_04/meetalign/tests.py
git commit -m "컴파일: AI 호출을 한 번으로 합치고 규칙 적발은 후보 1개로 승격한다"
```

- [ ] **Step 6: 리뷰 — 여기서 멈춘다**

fresh reviewer 1회. 집중 지점: 0022 데이터 보정(draft를 건드리지 않는지, 이미 `final_text`가 있는 레코드를 덮지 않는지, 공개 문장이 비는 경우가 없는지)과 `compile_meeting` 교체. Critical/Important가 있으면 고치고 다음 Task로 간다.

**Task 5 종료 시 테스트 수: 327** (345 − 삭제 27 + 신규 9)

---

### Task 6: 진행자 화면 — 입력 칸 제거와 두 묶음

**Files:**
- Modify: `APL_04/meetalign/views.py:390-427` (`_save_decision_edits` 축소), `:485-498` (`_current_choice`), `:502-607` (`decision_review` 컨텍스트)
- Modify: `APL_04/meetalign/templates/meetalign/decision_review.html`
- Test: `APL_04/meetalign/tests.py` (`DecisionReviewTests`)

**Interfaces:**
- Consumes: Task 5의 요약 키(`ambiguous`), Task 1의 `has_warning`
- Produces: `decision_review` 컨텍스트에 `ambiguous_rows: list`, `clear_rows: list`, `ambiguous_count: int`(기존 `warned_count` 대체). `rows`는 유지한다.

- [ ] **Step 1: 실패하는 테스트 3개를 쓰고 옛 테스트 3개를 지운다**

삭제 — `editing_a_spec_clears_the_warnings_it_fixes`, `saving_a_blank_what_leaves_the_item_untouched`, `dropping_every_candidate_leaves_the_item_with_none`.

```python
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

`views.py` — `_save_decision_edits`에서 명세 수정 블록과 후보 수정·버리기 블록을 지우고 `_apply_host_choice`만 부른다. `_current_choice`는 `not decision.has_warning`이고 `marked_undecided`가 아니면 `("", decision.final_text)`를 돌려준다(B 묶음의 접힌 '고치기'에 아무것도 선택돼 있지 않고, '직접 쓰기' 칸에 AI 문장이 채워진다). 컨텍스트에서 `warned_count` → `ambiguous_count`, `rows`를 `ambiguous_rows`(`row["decision"].has_warning`)와 `clear_rows`로 나눠 함께 넣고 `max_warnings`·`min_interpretations` 키를 지운다.

`decision_review.html` — 입력 칸 4개·확정 여부 라디오·후보 수정 input·버리기 체크박스 삭제. A 묶음: 제목 "확인이 필요한 결정 N개", `divergence_reason`을 굵게, `warnings`를 `record`에 작게, 후보 라디오(문장 + `근거: …` + `recommended`면 `badge processing`으로 "AI 추천"), 그다음 '직접 쓰기'·'다음 회의로 넘김'. B 묶음: 제목 "그대로 공개될 결정 M개", `final_text`를 보여주고 `<details><summary>고치기</summary>`에 '직접 쓰기'·'다음 회의로 넘김' 라디오. `{% block note %}`과 배지·안내에서 "경고"를 "확인 필요"로, `spec_line` 출력(67줄) 삭제.

- [ ] **Step 4: 전체 테스트가 통과하는지 확인한다**

Run: `cd APL_04 && python manage.py test meetalign -v 1`
Expected: `Ran 327 tests` + `OK`

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
- Consumes: Task 6의 `ambiguous_rows`/`clear_rows`
- Produces: `decision_compiler.CHOICE_DROP = "drop"`

- [ ] **Step 1: 실패하는 테스트 4개를 쓴다**

```python
def test_a_clear_item_can_be_rewritten_by_hand(self):
    decision = self.plain[0]
    self.client.post(self.url, {"action": "save",
                                "choice_%d" % decision.id: decision_compiler.CHOICE_CUSTOM,
                                "custom_text_%d" % decision.id: "민수가 금요일 18시까지 끝낸다"})
    decision.refresh_from_db()
    self.assertEqual(decision.final_text, "민수가 금요일 18시까지 끝낸다")

def test_dropping_an_item_removes_it_from_the_publish_set(self):
    for decision in (self.plain[0], self.warned[0]):
        self.client.post(self.url, {"action": "save",
                                    "choice_%d" % decision.id: decision_compiler.CHOICE_DROP})
        self.assertFalse(Decision.objects.filter(pk=decision.pk).exists())
        self.assertEqual(Interpretation.objects.filter(decision_id=decision.pk).count(), 0)
    self.assertEqual(self.client.get(self.url).context["draft_count"],
                     self.meeting.decisions.count())

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
    self.assertContains(self.client.post(self.url, {"action": "publish"}),
                        "공개할 항목이 없습니다")
```

- [ ] **Step 2: 실패를 확인한다**

Run: `cd APL_04 && python manage.py test meetalign.tests.DecisionConfirmTests -v 2`
Expected: FAIL — `AttributeError: ... CHOICE_DROP`

- [ ] **Step 3: 구현한다**

`decision_compiler.CHOICE_DROP = "drop"`. `views._apply_host_choice`에 분기를 더한다 — `raw == CHOICE_DROP`이면 `decision.status != Decision.STATUS_DRAFT`일 때 `"공개된 항목은 뺄 수 없습니다. 내용을 고치려면 '확정 문장 수정'을 쓰세요."`를 돌려주고, draft면 `decision.delete()` 후 즉시 `None`을 돌려준다(뒤이은 `save(update_fields=...)`를 타지 않게).

`decision_review.html` — A 묶음 라디오 맨 아래에 `이 결정 빼기 — 회의에 없던 결정이에요`를 기존 `meta` 스타일로, B 묶음 `<details>` 안 라디오에도 셋째로 붙인다.

- [ ] **Step 4: 전체 테스트가 통과하는지 확인한다**

Run: `cd APL_04 && python manage.py test meetalign -v 1`
Expected: `Ran 331 tests` + `OK`

- [ ] **Step 5: 커밋**

```bash
git add APL_04/meetalign/decision_compiler.py APL_04/meetalign/views.py APL_04/meetalign/templates/meetalign/decision_review.html APL_04/meetalign/tests.py
git commit -m "진행자 확정: 회의에 없던 결정을 빼는 선택지를 두 묶음에 모두 추가"
```

- [ ] **Step 6: 브랜치 전체 리뷰 — 여기서 멈춘다**

fresh reviewer 1회로 `main...feat/decision-candidates` 전체를 본다.

---

## 테스트 수 흐름

| Task | 삭제 | 신규 | 종료 시 | 전체 녹색 |
|---|---|---|---|---|
| 시작 | | | 325 | ✓ |
| 1 모델·0022 | 0 | 2 | 327 | ✓ |
| 2 미정 자동 문장 | 0 | 1 | 328 | ✓ |
| 3 `parse_items` 추가 | 0 | 11 | 339 | ✓ |
| 4 `safety_warnings` 추가 | 0 | 6 | 345 | ✓ |
| 5 교체·정리 (+리뷰) | 27 | 9 | 327 | ✓ |
| 6 진행자 화면 | 3 | 3 | 327 | ✓ |
| 7 이 결정 빼기 (+리뷰) | 0 | 4 | **331** | ✓ |
| 합계 | 30 | 36 | **331** | |

원래 계획의 "삭제 14 / 신규 20"은 Task 3·4가 옛 함수를 바로 교체한다는 전제였다. 모든 커밋을 녹색으로 유지하려면 새 테스트를 먼저 더하고 옛 테스트를 Task 5에서 지우므로, 같은 결과(331)에 삭제 30 · 신규 36으로 도달한다.

`fake_mode_runs_the_whole_flow_without_any_llm`은 기존 테스트의 재작성이어서 신규로 세지 않는다(스펙 §7이 신규 20개로 센 것은 이 한 건을 잘못 포함한 결과이고, 그만큼을 Task 1의 0022 보정 테스트가 메운다).

---

## Task 5 리뷰 후 fix pass (승인됨)

Task 5(`89f0697`) 직후 fresh reviewer(opus) 1회. 판정 **REQUEST CHANGES** — Critical 3 · Important 6 · Minor 5.
아래 1~5번과 M1을 사용자가 승인했다. 나머지 Minor(M2~M5)는 보류하고 기록만 남긴다.
각 수정은 TDD로 검증한다 — 결함을 재현하는 테스트를 먼저 쓰고 실패를 확인한 뒤 고치고, 끝에 전체 스위트를 돌린다.
**아직 코드는 수정하지 않았다. 이 섹션은 승인된 작업 목록이다.**

### 리뷰에서 깨끗하다고 확인된 것

- 0022가 draft 레코드를 건드리지 않는다 (`status__in=("published","undecided")` 필터).
- 0022가 이미 `final_text`가 있는 레코드를 덮지 않는다 (같은 필터의 `final_text=""` 조건).
- 0022의 `RunPython`이 역방향 안전하고 역사적 모델을 바르게 쓴다 — `apps.get_model`로 받고 상태값을
  상수 대신 문자열로 적었다. 전방 로직은 멱등이고 `AddField`와 같은 마이그레이션이라 함께 롤백된다.
- LLM 호출·파싱이 트랜잭션 밖, 저장만 원자적. `Decision.order`·`Interpretation.order` 유일성.
- 익명성 보장 전부 — `ack_counts`는 `user`를 조회하지 않고, 전원 응답 전에는 숫자를 컨텍스트에 담지 않는다.
- 플랜 Review Focus 1~4가 코드에서 처리됨(5번은 Task 7).

### 직접 실행으로 재현을 확인한 결함

```
C1: parse_items([{topic, ambiguous:true, candidates:[2개]}])   # why 누락
    → why=''  →  _resolve_item → warnings=[] / divergence_reason='' / final_text=''
    → has_warning False → is_settled True → publish_blockers 통과 (빈 문장으로 공개)
I1: parse_items([{topic, ambiguous:false, final:'...'}], source_text='전혀 다른 원문')
    → final 그대로 유지 (evidence가 없으면 근거 검증을 통째로 건너뛴다)
```

### 수정 1 — `publish_blockers`에 문장 유무 가드 (C1·C2 동시 차단)

- Files: `APL_04/meetalign/decision_compiler.py` (`publish_blockers`)
- 어떤 경로로 들어온 빈 문장이든 공개를 막는 마지막 그물.
  `return [d for d in decisions if not d.is_settled or not d.published_text]`
- C2(0022 이후 공개되는 옛 무경고 draft — 네 칸이 다 찼지만 `final_text`가 빈 레코드)도 이 가드가 덮는다.
- [ ] RED → GREEN: `test_an_item_without_a_sentence_cannot_be_published`
  — `warnings=[]`·`divergence_reason=""`·`final_text=""`인 draft를 만들고 `publish_blockers`에 잡히는지,
    `action=publish`가 거부되는지 본다.

### 수정 2 — 모호 항목의 사유를 비워 두지 않는다 (C1의 뿌리)

- Files: `APL_04/meetalign/decision_compiler.py` (`_resolve_item`)
- `return [], item["why"] or "AI가 다르게 이해할 수 있다고 봤어요", "", item["candidates"]`
- [ ] RED → GREEN: `test_an_ambiguous_item_without_a_reason_still_needs_the_host`
  — `why` 없는 `ambiguous:true` 응답으로 컴파일해 `divergence_reason`이 채워지고
    `has_warning` True·`is_settled` False가 되는지 본다.

### 수정 3 — 명확 항목은 근거가 없으면 승격한다 (I1)

- Files: `APL_04/meetalign/decision_compiler.py` (`parse_items`)
- `source_text`가 주어졌을 때는 빈 `evidence`도 검증 실패로 본다:
  `if final and normalized_source is not None and (not evidence or _normalize_for_match(evidence) not in normalized_source): final = ""`
- 후보 쪽의 빈 `evidence` 허용은 가짜 모드 통로이므로 그대로 둔다
  (`test_parse_items_matches_evidence_loosely_and_skips_blanks`가 못 박은 사양).
- [ ] RED → GREEN: `test_parse_items_requires_evidence_for_a_clear_item`
  — `evidence` 없는 `ambiguous:false` 항목에 `source_text`를 주면 `final`이 비는지 본다.

### 수정 4 — 진행자 화면이 AI 모호 항목에 선택지를 그린다 (C3)

- Files: `APL_04/meetalign/templates/meetalign/decision_review.html` (`:70`, `:149`, `:154`)
- AI 모호 항목은 `warnings`가 비고 `divergence_reason`만 차므로, 지금은 `row.blocking`이 True인데
  라디오가 하나도 렌더되지 않아 공개가 영구 차단된다(FAKE 데모가 공개 직전에 멈춘다).
  세 곳의 `row.decision.warnings`를 `row.decision.has_warning`으로 바꾸고 사유 목록 위에
  `divergence_reason`을 한 줄 출력한다. Task 6이 이 템플릿을 전면 교체하지만, 지금 커밋도 동작해야 한다.
- [ ] RED → GREEN: `test_the_screen_offers_choices_for_an_ai_only_ambiguous_item`
  — FAKE 모드로 컴파일한 뒤 모호 항목의 `choice_<id>` 라디오와 `divergence_reason` 문구가 HTML에 있는지 본다.

### 수정 5 — 남은 Important 다섯 건

| # | 내용 | Files | 테스트 |
|---|---|---|---|
| I3 | `max_warnings` 컨텍스트 키를 지웠는데 문구가 남아 **"회의당 최대 개."**로 렌더된다. `{% block note %}`를 새 규칙 설명으로 교체 | `decision_review.html:3-5`, `:25-26` | [ ] `test_the_note_block_does_not_mention_a_removed_limit` |
| I4 | 헤더가 `warnings`만 세어 "경고 0개 · 확정 안 한 항목 2개"로 모순되게 보인다 → `has_warning` 기준 | `views.py:544` | [ ] `test_the_header_counts_items_that_need_the_host` (Task 6에서 키가 `ambiguous_count`로 바뀌면 같은 사실을 그 키로 본다) |
| I5 | `Interpretation` docstring이 "가상 청자의 해석"으로 남았다 → "그대로 공개될 확정 문장 후보"(스펙 §5.3 미이행) | `models.py:224-229` | 테스트 없음(문서) |
| I6 | 0022 보정이 **undecided** 행에 "다음 회의로" 꼬리말을 붙이지 않아 팀원 화면에서 미결정이 보통 결정처럼 읽힌다 | `migrations/0022_decision_candidates.py` | [ ] `test_0022_marks_backfilled_undecided_rows_as_carried_over` |
| I2 | 컴파일 버튼을 두 번 누르면 `exists()` 검사가 트랜잭션 밖이라 `IntegrityError` → 500. **이 브랜치가 만든 문제는 아니지만**(원래부터 그랬다) 싸게 막는다 — 쓰기 블록을 `except IntegrityError`로 감싸 `DecisionCompileError`로 바꾼다 | `decision_compiler.py:350-359` | [ ] `test_compiling_twice_at_once_is_refused_not_crashed` |

### 수정 M1 — 옛 전제가 남은 주석·docstring 5곳

사라진 규칙("빈칸 경고", "회의당 최대 5개")을 설명하는 주석이 C1·C2를 정상처럼 보이게 하므로 함께 고친다.

- `models.py:139-140` — "빈칸이나 모호어가 있으면 … (회의당 최대 5개까지만 만든다)"
- `models.py:179-180` — "경고가 없는 항목은 명세 자체가 확정 내용이라 비어 있을 수 있다" (새 흐름에서 무경고 항목은 `final_text`를 반드시 갖는다)
- `decision_compiler.py:112-113` — "'경고 최대 5개' 같은 제한도 여기서 지켜야 하기 때문이다"
- `decision_compiler.py:411-413` — "진행자는 경고가 붙은 항목마다 …" (기준이 `has_warning`으로 바뀌었고 B 묶음도 '고치기'를 받는다)
- `decision_compiler.py:108` — `DecisionCompileError` "명세/해석으로 못 읽었을 때" (호출이 한 번으로 합쳐졌다)

테스트 없음(문서).

### 보류한 Minor (기록만)

- **M2** 0022에서 네 칸이 전부 빈 공개 레코드는 `continue`로 빠져 빈 문장이 남는다. 플레이스홀더 문구와 `bulk_update` 묶기 제안.
- **M3** `safety_warnings`의 중복 제거 키가 사유 문자열 포맷(`rsplit(": ")`)에 의존한다. `(종류, 토큰)` 튜플을 키로 쓰는 편이 안전하다.
- **M4** `_generate_parsed(prompt, parser, what_failed)`의 일반화가 1+N 호출 시절의 잔재다. 호출자가 하나뿐이고 `what_failed`는 항상 `"결정"`.
- **M5** `decision_review.html:136`의 "근거 없음 (가짜 응답 모드)" 문구가 실제 모드에서도 뜰 수 있다(I1과 같은 뿌리). "근거를 찾지 못했어요"로 중립화 제안.

### fix pass 후 테스트 수

fix pass가 새 테스트 7개를 더한다(I5·M1은 문서라 테스트 없음): **327 → 334**.
이후 Task 6은 삭제 1 · 신규 3(→ **336**), Task 7은 신규 4(→ **340**)가 된다.

340은 원래 계획의 331과 다르다. 차이는 둘이다.
(1) Task 5에서 찾은 실제 결함(날짜·시각을 한 묶음으로 세어 "금요일 18시까지"가 충돌로 잡히던 문제)에 테스트 2개,
(2) 이 fix pass에 테스트 7개. 둘 다 계획을 쓸 때 몰랐던 결함을 막는 테스트이므로, 숫자를 맞추려고 접지 않고 늘렸다.
