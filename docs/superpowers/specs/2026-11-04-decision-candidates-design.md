# 진행자 확정 단계 재설계 — 확정 문장 후보

- 날짜: 2026-11-04
- 브랜치: `feat/decision-candidates` (upstream main `bec93c6`, PR #41 머지 후)
- 대상: `APL_04/meetalign` 결정 컴파일러의 ② 해석 후보 생성과 ③ 진행자 확정

## 1. 왜 고치는가

지금 진행자는 같은 일을 두 번 한다.

1. **기준이 둘이다.** 진행자가 누가·무엇을·언제까지·완료 기준 네 칸을 직접 채우고, 그와 별도로
   확정 문장을 고른다. 공개될 때는 경고가 붙은 항목만 `final_text`를 쓰고 나머지는 명세 한 줄
   (`spec_line`)을 쓴다 — 팀원이 보는 문장의 출처가 항목마다 다르다.
2. **빈칸만으로 경고가 난다.** `_blank_warnings`가 담당자·기한·완료 기준의 빈칸마다 경고를
   붙이므로 거의 모든 결정이 "경고 있음"이 되고, 경고라는 표시가 정보를 잃는다.
3. **해석 후보가 쓸모없다.** 후보가 "~라는 뜻" 수준의 해설이라 그대로 공개할 수 없고, 빈칸이
   많은 항목에는 아무 도움이 되지 않는다.

## 2. 목표

AI가 모호한 결정을 찾아 **그대로 공개될 확정 문장 후보 2~3개**를 만들고, 진행자는 읽고 탭
한 번으로 고른다. 탭 몇 번으로 회의의 모호함이 정리되어야 한다.

성공 기준:

- 공개되는 문장은 **언제나 `final_text` 하나**다. 폴백도 예외도 없다.
- 진행자 화면에 누가·무엇을·언제까지·완료 기준 입력 칸이 없다.
- 명확한 결정은 탭 없이 공개 목록에 들어간다.
- AI 추천은 배지뿐이고, 미리 선택되어 있지 않다.

## 3. 확정된 전제

사용자와 합의가 끝난 사항이며 이 설계의 입력이다.

- **해석 단위**: 결정 하나에 완성된 확정 문장 후보 2~3개. 칸별 칩 선택이 아니다. 후보마다
  누가·무엇을·언제까지·완료 기준을 원문에서 찾아 채운 실행 가능한 문장이어야 한다.
- **근거 필수**: 후보의 근거 문장이 전사 원문에 그대로 없으면 그 후보는 버린다(기존 검증 유지).
- **모호 판단은 AI**. 규칙은 안전망으로 모호어·숫자 충돌만 확인하고, AI가 명확하다고 했어도
  규칙이 걸리면 진행자에게 묻는다. 빈칸만으로는 경고하지 않는다.
- **고정 선택지 2개**: '다음 회의로 넘김'과 '직접 쓰기'는 AI 후보가 아니라 항상 붙는다.
- **동작 불변**: 팀원 확인(`decision_ack`), 익명 집계, 미확인 명단, 배지, 미결정 이월.
- **구현 방향**: 기존 모델 유지 + AI 호출을 한 번으로 합침. 마이그레이션 0022 하나, 칸 추가만.
- **금지**: `services.py` 수정(`_generate_clean`, `_normalize_for_match`, `_enabled` 재사용),
  새 CSS 프레임워크·CDN.
- **톤**: "틀렸다"가 아니라 "다르게 이해할 수 있다".

## 4. 코드 현실과의 차이 (설계가 반드시 처리할 것)

코드를 읽고 확인한, 합의안이 전제한 것과 실제가 다른 지점이다.

| # | 사실 | 이 설계의 처리 |
|---|---|---|
| 1 | `models.py:216` `published_text = final_text or spec_line`, `decision_ack.html:27`은 미정 항목에 `what`을, `meeting_detail.html:39`은 `spec_line`을 쓴다. `views.py:367`은 '아직 미정' 선택 시 `final_text`를 비운다 | 폴백 삭제. '다음 회의로 넘김'을 고르면 서버가 `final_text`를 자동 생성해 채운다 (§5.4) |
| 2 | `views._recompute_warnings`는 진행자가 고친 네 칸을 다시 읽어 경고를 지운다. `_cross_warnings`도 who·due 필드를 전제로 한다 | 입력 칸과 함께 두 함수 모두 삭제 (§5.3, §7) |
| 3 | `compile_meeting`은 컴파일 1회 + 경고 항목마다 1회(1+N) 호출한다. `soft_fail=True`와 `few_candidates`가 그 구조에 묶여 있다 | 호출 1회로 통합하고, 관대 처리를 항목 단위로 옮긴다 (§5.2) |
| 4 | 후보 텍스트 수정 input(`interpretation_<id>`)과 '버리기' 체크박스(`drop_interpretation_<id>`)가 있다 | 둘 다 삭제. 후보는 읽기 전용 (§5.1) |
| 5 | who/what/due_text/done_criteria/confirm_state를 읽는 곳은 `admin.py`와 템플릿 세 곳뿐이다. `Checkpoint` 모델은 어떤 뷰도 쓰지 않고, 사양서도 모델 수준으로만 적는다(`상세판.md:101`) | 필드는 남기고 신규 컴파일에서 비운다. `admin.py`의 `who` 참조를 `final_text`로 교체 (§5.3) |

## 5. 설계

### 5.1 진행자 화면 (`decision_review.html`)

컴파일 전은 그대로다(`결정 컴파일 실행` 버튼 하나). 컴파일 후 화면을 두 묶음으로 나눈다.

**A. "확인이 필요한 결정 N개"** — `warnings` 또는 `divergence_reason`이 있는 항목.

```
③ 로그 보관 기간                              [확인 필요]
  "충분히"가 30일인지 90일인지 갈릴 수 있습니다      ← divergence_reason (굵게)
  · 모호어 '나중에'가 들어 있습니다                  ← warnings (작게, 있을 때만)

○ 민수가 12월 5일까지 로그 보관 기간을 30일로 바꾸고,   [AI 추천]
   스테이징 로그가 30일 뒤 지워지는 것을 확인한다
   근거: "일단 한 달 정도면 충분할 것 같은데요"

○ 민수가 12월 5일까지 로그 보관 기간을 90일로 바꾸고,
   감사 로그 3개월 요건을 문서에 남긴다
   근거: "감사 때문에 분기 단위로 봐야 한다고 들었어요"

○ 직접 쓰기  [____________________]
○ 다음 회의로 넘김
```

- 후보는 **읽기 전용 문장**이다. 라디오 하나가 곧 확정이다.
- `AI 추천`은 배지뿐이며 `checked`를 걸지 않는다. 진행자가 읽고 직접 탭해야 한다.
- 고정 선택지 '직접 쓰기'·'다음 회의로 넘김'은 후보 수와 무관하게 항상 있다. 후보가 0개여도
  이 둘로 처리할 수 있다.

**B. "그대로 공개될 결정 M개"** — `warnings`와 `divergence_reason`이 모두 빈 항목. AI가 쓴
`final_text` 문장을 보여주고, 그 아래 **접힌 '고치기'**를 둔다. 두 묶음 자체는 모두 펼친 상태다.

```
② 배포 스크립트 정리
  민수가 금요일 18시까지 배포 스크립트를 정리하고,
  스테이징에서 한 번 돌려 성공하는 것을 확인한다
  ▸ 고치기                                    ← 접혀 있음 (<details>)
```

펼치면 라디오 세 개가 나온다. 평소에는 접혀 있으므로 **명확한 결정의 탭 0번은 유지된다.**

| 선택지 | 동작 |
|---|---|
| 직접 쓰기 | 입력한 문장으로 `final_text`를 교체한다. 기존 `CHOICE_CUSTOM` 경로를 그대로 쓴다 |
| 다음 회의로 넘김 | `marked_undecided=True` + 미정 문장 자동 생성(§5.4). 기존 `CHOICE_UNDECIDED` 경로 |
| 이 결정 빼기 | 그 `Decision`을 삭제한다. AI가 회의에 없던 결정을 만들어 낸 경우용 |

'이 결정 빼기'는 새 선택지이므로 `decision_compiler.CHOICE_DROP = "drop"`을 추가하고
`_apply_host_choice`에서 처리한다. **`status`가 `draft`인 항목에서만** 가능하다 — 공개된 뒤에는
팀원 확인 기록이 딸려 있으므로 거부하고, 내용을 바꾸려면 기존 '확정 문장 수정'(revise)을 쓴다.
삭제는 `Interpretation`까지 cascade로 함께 지워진다.

A 묶음에는 '고치기'를 따로 두지 않는다 — 이미 라디오에 '직접 쓰기'와 '다음 회의로 넘김'이
펼쳐져 있다.

> **열린 항목 (승인 필요)**: '이 결정 빼기'를 A 묶음 라디오에도 넷째 선택지로 붙일지. AI가
> 회의에 없던 결정을 만들어 내는 일은 모호 판정 항목에서도 일어날 수 있어 대칭이 자연스럽지만,
> 요청받은 범위는 B 묶음뿐이다. 승인 없이는 B 묶음에만 넣는다.

**제거**: 네 입력 칸, 확정 여부 라디오, 후보 수정 input, '버리기' 체크박스.

**유지**: 저장 · 확정 명세 공개(미확정 차단 동일) · 명세 지우고 다시 컴파일 · 공개 취소 ·
공개 후 확정 문장 수정 · "아직 확인하지 않은 팀원" 카드 · 익명 집계.

**문구**: 화면에서 "컴파일 경고"를 "확인 필요"로 바꾼다. 모델 필드명 `warnings`는 그대로 둔다.
스타일은 기존 토큰(`card elevated`, `badge`, `record`, `meta`, `success`, `notice`)만 쓴다.

### 5.2 AI 호출 — 한 번, 통합 응답

전사 원문 전체를 한 번 보내고 결정 배열을 받는다.

**응답 스키마**

```json
[
  { "topic": "로그 보관 기간",
    "ambiguous": true,
    "why": "\"충분히\"가 30일인지 90일인지 갈립니다",
    "candidates": [
      { "text": "민수가 12월 5일까지 로그 보관 기간을 30일로 바꾸고, 스테이징 로그가 30일 뒤 지워지는 것을 확인한다",
        "evidence": "일단 한 달 정도면 충분할 것 같은데요",
        "recommended": true },
      { "text": "민수가 12월 5일까지 로그 보관 기간을 90일로 바꾸고, 감사 로그 3개월 요건을 문서에 남긴다",
        "evidence": "감사 때문에 분기 단위로 봐야 한다고 들었어요" } ] },

  { "topic": "배포 스크립트 정리",
    "ambiguous": false,
    "final": "민수가 금요일 18시까지 배포 스크립트를 정리하고, 스테이징에서 한 번 돌려 성공하는 것을 확인한다",
    "evidence": "배포 스크립트는 제가 금요일까지 정리할게요" }
]
```

**프롬프트가 요구하는 것**

- `text`/`final`은 **그대로 공개될 문장**이다. 누가·무엇을·언제까지·완료 기준이 문장 안에 들어가고,
  원문에 없는 내용은 넣지 않는다.
- "~라는 뜻", "A일 수도 B일 수도" 같은 해설체 금지. 어느 후보가 맞는지도 쓰지 않는다.
- `why`는 "다르게 이해할 수 있는 이유" 한 줄. "틀렸다"로 쓰지 않는다.
- `evidence`는 원문 문장을 토씨 하나 틀리지 않고 복사한다. 없는 문장을 쓰면 그 후보는 버려진다.
- `recommended`는 근거가 가장 강한 **하나**에만 붙인다.
- 모호한 것이 많으면 가장 중요한 5개에만 `ambiguous`를 붙이도록 유도한다(서버 상한은 없다).

**파싱 규칙** — `parse_decisions` 하나로 통합하고, 관대 처리는 항목 단위로 한다.

| 상황 | 처리 |
|---|---|
| `topic` 비었음 | 그 항목 버림 (기존 `what` 없으면 버림과 동일) |
| `ambiguous:false` + `final` 없음 또는 근거 검증 실패 | 모호로 승격, 후보 0개 |
| `ambiguous:true` + 근거 통과 후보 0개 | 모호, 후보 0개 (`few_candidates`에 집계) |
| `recommended`가 여러 개 | 첫 번째만 인정 |
| 후보 3개 초과 | `MAX_INTERPRETATIONS=3`에서 자름 |
| 중복 후보 문장 | 기존과 같이 버림 |
| JSON 배열 자체를 못 읽음 | `PARSE_RETRIES=1`로 1회 재시도 → `DecisionCompileError` (회의 단위 실패, 아무것도 저장하지 않음) |
| `services._generate_clean`이 `None`/`OSError` | 기존과 같이 FAKE 응답으로 전체 흐름을 잇는다 |

`compile_meeting`이 돌려주는 요약은 `{"decisions", "ambiguous", "few_candidates"}`가 된다 —
`warned`를 `ambiguous`로 바꾸고, 상한이 없어지므로 `dropped_warnings`는 없앤다.

`MIN_INTERPRETATIONS=2`는 폐기한다 — 후보가 1개여도 고정 선택지 2개와 함께 탭할 수 있으므로,
집계 기준을 "후보 0개"로 바꾼다. `MAX_WARNED_DECISIONS=5`도 폐기한다: 모호 판정이 AI로 넘어간
뒤에 서버가 앞 5개만 남기면, 모호하다고 판정된 항목이 진행자 동의 없이 조용히 공개되거나 자동
이월된다. 상한은 프롬프트로만 유도한다.

**규칙 안전망**은 `ambiguous:false` 항목의 **`final` 문장과 `evidence`(원문 근거) 둘 다** 스캔한다.
모호 항목은 이미 진행자에게 묻고 있으므로 중복 검사하지 않는다.

- `final`에서 걸린 경우 — AI가 쓴 확정 문장 자체에 모호함이 남아 있다.
  사유 예: `모호어 '나중에'가 들어 있어요`
- `evidence`에서 걸린 경우 — 원문은 모호한데 AI가 그것을 혼자 해소해 버린 것으로 본다. 그 해소가
  맞을 수도 있지만, 회의에서 정해지지 않은 것을 AI가 정한 셈이므로 진행자가 확인해야 한다.
  사유 예: `원문에 '다음 주쯤'이 있어요`

  > 원문 "다음 주쯤 할게요" → AI `final` "다음 주 금요일까지 …", `ambiguous:false`
  > → 규칙이 `evidence`에서 `쯤`을 잡아 **확인 필요로 승격**한다.

어느 쪽이든 걸리면 처리는 같다:

1. `warnings`에 사유를 넣어 그 항목을 확인 필요로 올린다,
2. AI가 쓴 `final` 문장을 `Interpretation` 한 개로 저장해 "이 문장 그대로 공개" 후보로 보여준다,
3. `final_text`는 비워 둔다 → 진행자가 탭해야 공개된다.

살아남는 규칙은 `_ambiguous_warnings`(`AMBIGUOUS_PHRASES`)와 `_conflict_warnings`(같은 문장 안의
날짜·숫자 충돌)뿐이고, 둘을 `final`과 `evidence`에 각각 적용한다. 같은 사유가 양쪽에서 나오면
기존처럼 한 번만 남긴다.

명확 항목의 `evidence`는 규칙 스캔과 근거 검증에만 쓰고 저장하지 않는다 — 승격된 항목에서만
`Interpretation.evidence`로 남아 화면에 "근거: …"로 보인다. `Decision`에 근거 칸을 더 만들지 않기
위한 선택이며, 승격되지 않은 명확 항목은 근거를 보여줄 자리가 화면에 없다.

### 5.3 모델과 마이그레이션

**마이그레이션 0022 — 칸 2개 + 데이터 보정 1개**

```python
Decision.divergence_reason = TextField(blank=True, default="")   # "왜 갈릴 수 있나" 한 줄 (AI)
Interpretation.recommended  = BooleanField(default=False)        # 'AI 추천' 배지

# RunPython (역방향 no-op): final_text가 빈 published/undecided 레코드에 기존 네 칸으로 만든
# 명세 한 줄을 채운다. published_text의 폴백을 지우면 그 레코드들의 공개 문장이 사라지기 때문이다.
# draft 레코드는 다시 컴파일하면 되므로 건드리지 않는다.
# 문장 생성은 모듈 수준 함수 spec_sentence(who, what, due, done)으로 빼서 단위 테스트한다.
```

**마이그레이션이 필요 없는 변경**

| 대상 | 변경 |
|---|---|
| `Decision.published_text` | `final_text or spec_line` → `final_text` |
| `Decision.spec_line` | 삭제. 마지막 사용처 `meeting_detail.html:39`(이월 목록)를 `final_text`로 교체 |
| `Decision.is_settled` | `final_text` 또는 `marked_undecided` 또는 `not (warnings or divergence_reason)` |
| `Decision.has_warning` | `bool(warnings or divergence_reason)` |
| `Decision.what` | 응답의 `topic`(안건 한 줄)을 **계속 여기에 저장한다**. 화면의 카드 제목과 `admin` 목록이 쓴다 |
| who·due_text·done_criteria·confirm_state | 필드 유지, 신규 컴파일에서는 **이 네 칸만** 비운다(기존 레코드 보존용) |
| `Interpretation` docstring | "가상 청자의 해석" → "그대로 공개될 확정 문장 후보" |
| `admin.py` | `list_display`·`search_fields`의 `who` → `final_text` |
| `decision_compiler` 상수 | `MIN_INTERPRETATIONS`·`MAX_WARNED_DECISIONS` 삭제, `CHOICE_DROP="drop"` 추가. `MAX_DECISIONS=12`·`MAX_INTERPRETATIONS=3`·`PARSE_RETRIES=1`·`AMBIGUOUS_PHRASES`·`CHOICE_CANDIDATE_PREFIX`·`CHOICE_CUSTOM`·`CHOICE_UNDECIDED` 유지 |

**삭제되는 함수**: `_blank_warnings`, `_cross_warnings`, `_interpretation_prompt`,
`interpretations_for`, `_fake_interpretations`, `views._recompute_warnings`,
`views._save_decision_edits`의 명세 수정·후보 편집 블록.

### 5.4 미정 항목의 공개 문장

'다음 회의로 넘김'을 탭하면 서버가 `final_text`를 자동 생성한다(진행자 타이핑 없음).

```
final_text = "<topic> — 이번 회의에서 결론이 나지 않았음 (다음 회의로)"
marked_undecided = True
```

이로써 `published_text`는 모든 상태에서 `final_text` 하나가 되고, `decision_ack.html`의 분기와
`meeting_detail.html`의 `spec_line` 참조가 사라진다.

### 5.5 FAKE 데이터 (LLM 키 없는 시연)

`FAKE_DECISIONS`를 새 스키마의 고정 응답으로 교체한다 — 모호 2건(각 후보 2개, `recommended`
하나) + 명확 1건. `evidence`는 빈 문자열로 둬서 근거 검증을 건너뛰는 현재 통로를 유지한다
(가짜 모드에는 대조할 원문이 없다). 문장은 회의.txt 상황(로그 보관 기간, 배포 스크립트,
QA 범위)으로 구체적으로 쓴다. `_fake_interpretations`는 삭제한다 — 후보가 통합 응답 안에 들어온다.

## 6. 동작이 바뀌지 않는 것

`decision_ack`의 확인·달랐어요, 익명 집계와 전원 응답 전 비공개, 미확인 명단, 회의 목록 배지
(`decision_status`), 미결정 이월과 '처리됨', 공개 취소, 공개 후 확정 문장 수정과 확인 기록 초기화,
권한(진행자 전용 403/404). `services.py`는 손대지 않는다.

## 7. 테스트

**삭제 (14개)** — 전제가 사라진 것
`DecisionWarningTests`: `blank_fields_each_raise_their_own_warning`,
`fully_filled_spec_has_no_warning`, `same_owner_with_different_due_dates_warns_on_both_items`,
`same_owner_with_the_same_due_date_does_not_warn`,
`different_owners_are_not_compared_with_each_other`, `at_most_five_items_keep_their_warnings`,
`items_without_warnings_do_not_use_up_the_five_slots` /
`DecisionReviewTests`: `editing_a_spec_clears_the_warnings_it_fixes`,
`saving_a_blank_what_leaves_the_item_untouched`,
`dropping_every_candidate_leaves_the_item_with_none` /
`DecisionCompileTests`: `dropped_warning_count_is_reported` /
`DecisionModelTests`: `spec_line_shows_blank_fields_as_question_marks`,
`published_text_falls_back_to_the_spec_line` /
`DecisionParseTests`: `parse_decisions_keeps_blank_who_and_due`

**재작성 (12개)** — 같은 관심사, 새 스키마
`parse_decisions`의 스키마 읽기·코드펜스 견디기·`topic` 없는 항목 버리기·후보 3개 제한·중복 후보
제거·깨진 JSON 빈 배열(6) / 근거 검증 3개(불일치 폐기, 공백 무시 비교, 빈 evidence 건너뛰기)를
통합 파서 경로로 이동 / `fake_mode_runs_the_whole_flow`와 `compile_uses_the_llm_response` 숫자·호출
가정 갱신(2) / `published_text`가 `final_text`와 같고 폴백이 없음(1)

**그대로 통과해야 하는 것**
`DecisionConfirmTests`(탭 흐름 불변), `DecisionPublishTests`, `DecisionReviseTests`,
`DecisionUnpublishTests`, `DecisionAckTests`, `DecisionPendingTests`. setUp에서 `Decision`을 직접
만드는 곳만 `divergence_reason`을 함께 넣는다.

**새 테스트 (19개)**

1. LLM 호출이 한 번이다 (`_generate.call_count == 1`)
2. 명확 항목 — `final_text`가 AI 문장으로 차고, 후보 0개, `is_settled` True
3. 모호 항목 — `divergence_reason` 저장, 후보 2~3개, `final_text` 빈 채로 blocking
4. `recommended`는 하나만 True, 여러 개 오면 첫 번째만
5. 추천이 미리 선택되지 않는다 — 후보 라디오에 `checked`가 없다
6. 규칙 승격 — 명확 판정 문장에 모호어가 있으면 `warnings`에 사유 + AI 문장이 후보 1개로 올라가고 blocking
7. 규칙은 모호 항목의 후보를 스캔하지 않는다
8. 명확 항목의 `evidence`가 원문에 없으면 모호로 승격, 후보 0개
9. 항목 단위 관대 — 한 결정의 후보만 깨져도 나머지는 정상 저장
10. 미정 자동 문장 — '다음 회의로 넘김' 탭에 `final_text`가 차고, 팀원 화면·이월 목록에 그 문장이 보인다
11. 화면에 입력 칸이 없다 — `who_`/`due_`/`done_`/`confirm_state_`/`interpretation_`/`drop_interpretation_` 이름이 HTML에 없다
12. 서버도 명세 수정 POST를 무시한다 (`who_<id>`를 보내도 저장되지 않는다)
13. 상한 폐기 — 모호 8개면 8개 모두 확인 대상
14. FAKE 모드가 새 스키마로 전체 흐름을 돈다 (모호 2 + 명확 1, 추천 배지 각 1개)
15. `spec_sentence()`가 기존 네 칸으로 명세 한 줄을 만든다 (0022 데이터 보정 로직)
16. 근거 승격 — 원문 `"다음 주쯤 할게요"` + AI `final` `"다음 주 금요일까지 …"` + `ambiguous:false`
    → `warnings`에 `원문에 '다음 주쯤'이 있어요`가 붙고 확인 필요로 올라가며, AI 문장이 후보 1개가
    되고 `final_text`는 빈다
17. B 묶음 '고치기' — 직접 쓰기로 `final_text`가 교체된다 (경고 없는 항목에도 choice가 적용된다)
18. B 묶음 '이 결정 빼기' — 그 `Decision`과 `Interpretation`이 삭제되고 공개 대상에서 빠진다
    (`draft_count`가 줄고 published 목록에 없다)
19. 공개된 뒤에는 '이 결정 빼기'가 거부된다 — 레코드가 남고 사유가 화면에 보인다

`tests.py`의 테스트는 현재 325개다. 삭제 14 · 신규 19 · 재작성 12(수량 불변)이므로 재설계 후
330개가 된다.

## 8. 범위에 없는 것

- `services.py` 수정, 새 CSS 프레임워크·CDN 도입
- `Checkpoint`(Phase E) 화면 구현
- 블라인드 투표 모드 복원(보류 상태 유지)
- 팀원 화면에 후보·근거 노출 — 후보는 진행자 참고용이라는 원칙 유지

## 9. 위험과 대응

| 위험 | 대응 |
|---|---|
| 통합 응답이 커서 파싱 실패율이 오른다 | 1회 재시도 후 회의 단위 실패(기존과 동일). 항목 내부 손상은 항목 단위로 관대 처리 |
| AI가 거의 전부를 모호로 판정해 탭이 많아진다 | 프롬프트로 상위 5개 유도. 서버는 막지 않는다(조용한 공개가 더 나쁘다) |
| 근거 검증이 후보를 전부 버려 고를 것이 없다 | 고정 선택지 2개가 항상 있어 처리 가능. `few_candidates`로 집계해 진행자에게 알린다 |
| 기존 published 레코드의 공개 문장이 빈다 | 0022 데이터 보정으로 네 칸에서 문장을 만들어 채운다 |
