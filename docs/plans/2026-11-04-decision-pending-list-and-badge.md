# 결정 확인 미확인 명단 + 회의 목록 배지 전환 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 진행자 화면에 "아직 결정을 확인하지 않은 팀원" 명단을 추가하고, 회의 목록 배지를 퀴즈 기준에서 결정 확인 기준으로 전환한다.

**Architecture:** 새 모듈 `meetalign/decision_status.py`가 `quiz_status.py`의 구조(같은 함수명·같은 고정 쿼리 수·같은 `badge_text`)를 미러링하되 집계 소스만 `CheckQuestion`/`Answer` → `Decision(status=published)`/`DecisionAgreement`로 바꾼다. 이름이 담긴 조회는 `_require_host` 통과 뒤에만 호출하고, 숫자만 담는 조회는 팀 홈에서 쓴다. 모델 변경·마이그레이션 없음.

**Tech Stack:** Django, `django.test.TestCase`, 기존 CSS 클래스(`pending-badge`, `pending-clear`, `progress-row`, `progress-track`, `progress-fill`)

**Spec:** 설계는 이 계획서 작성 직전 대화에서 확정됐다(별도 스펙 문서 없음 — bounded 작업). 확정된 기준은 아래 Global Constraints에 전부 옮겨 적었다.

## Global Constraints

- `meetalign/services.py` 수정 금지. `meetalign/services/` 패키지 생성 금지.
- 모델 변경·새 마이그레이션 금지. 필요해 보이면 멈추고 이유를 보고한다.
- `quiz_status.py` 삭제 금지 — `question_review`가 계속 사용한다. `meeting_list`에서 참조만 끊는다.
- 새 CSS 프레임워크·CDN 금지. 기존 배지 CSS(`pending-badge`, `pending-clear`)를 재사용한다.
- 완료 판정: 공개(`Decision.STATUS_PUBLISHED`) 항목 **전부**에 `DecisionAgreement`가 있는 팀원 = 완료. `kind`는 무관(`ack`/`differ` 둘 다 확인으로 집계).
- `undecided` 항목은 분모·분자 양쪽에서 제외(`status=published` 필터로 자연히 제외).
- 진행자(`Meeting.host`)는 집계 대상에서 제외. `host_id`가 `None`인 회의는 팀 멤버 전원을 대상으로 한다(`quiz_status` 관례 승계).
- 공개 항목이 0개인 회의는 배지 없음(`pending_count_for_meetings`가 `None`).
- 집계 대상이 0명이면 `pending=0` → "✓ 모두 확인" 표시(`quiz_status` 관례 승계).
- 명단에는 이름 + 진척도(`2/5`)만. 누가 `differ`를 골랐는지는 **어떤 화면·컨텍스트에도** 담지 않는다. `decision_compiler.ack_counts()`는 수정하지 않는다.
- 톤: "틀렸다"가 아니라 "다르게 이해했다". 개인 이름 노출 최소화.
- 툴팁 문구(정확히): `아직 N명이 결정을 확인하지 않았어요`
- 전원 완료 문구(정확히): `✓ 모두 확인`
- 테스트는 TDD — 반드시 실패를 먼저 확인한 뒤 구현한다.
- 기존 테스트 **344개가 줄면 안 된다**. 테스트 삭제 금지(수정·이름 변경은 허용).
- 커밋 메시지는 한국어. `push`는 하지 않는다. `.env`·키·비밀번호 커밋 금지.

## Review Focus

- **공개 항목이 하나도 없는데 `DecisionAgreement`가 남아 있는 회의** (공개 취소 후): 분모가 0이므로 배지는 숨겨야 하고, 진행자 화면 명단도 비어야 한다. → Task 1·2 테스트로 고정.
- **`host_id`가 `None`인 회의**: `_require_host`가 팀원 누구나 통과시키므로 명단이 아무에게나 보일 수 있다. 집계 대상은 팀 전원이어야 하고(진행자 없음), 크래시 없이 동작해야 한다. → Task 1 테스트로 고정.
- **같은 팀원이 같은 항목을 다시 눌러 `ack`→`differ`로 바꾼 경우**: `update_or_create`라 레코드 수가 늘지 않으므로 완료 상태가 유지되어야 한다(`differ`로 바꿨다고 미확인으로 돌아가면 안 된다). → Task 1 테스트로 고정.
- **다른 회의의 `DecisionAgreement`가 섞여 집계되는 것**: 같은 팀에 회의가 여러 개면 `decision__meeting_id`로 분리돼야 한다. → Task 2 쿼리 수/정확도 테스트로 고정.
- **회의 개수가 늘 때 쿼리 수 증가(N+1)**: 팀 홈은 전원이 보는 화면이라 회의 수와 무관하게 고정 쿼리여야 한다. → Task 2 테스트로 고정.

---

### Task 1: 진행자 화면 "아직 확인하지 않은 팀원" 명단

**Files:**
- Create: `APL_04/meetalign/decision_status.py`
- Modify: `APL_04/meetalign/views.py` (import 줄 15, `decision_review` 본문 — `_require_host` 호출 직후와 `render()` 컨텍스트)
- Modify: `APL_04/meetalign/templates/meetalign/decision_review.html` (compiled 분기의 메인 카드 뒤, 파일 끝 `{% endif %}{% endblock %}` 앞)
- Test: `APL_04/meetalign/tests.py` (새 클래스 `DecisionPendingTests`, `DecisionAckTests` 뒤에 추가)

**Interfaces:**
- Consumes: `models.Decision.STATUS_PUBLISHED`, `models.DecisionAgreement`, `models.Team.members`, `Meeting.host_id`, `views._require_host`
- Produces (Task 2가 의존):
  - `decision_status.BADGE_MAX = 99`
  - `decision_status.badge_text(count: int) -> str`
  - `decision_status._published_counts(meeting_ids) -> dict[int, int]`
  - `decision_status._acked_counts(meeting_ids) -> dict[tuple[int, int], int]`
  - `decision_status._member_ids_by_team(team_ids) -> dict[int, list[int]]`
  - `decision_status._target_ids(meeting, member_ids_by_team) -> list[int]`
  - `decision_status.participant_count(meeting) -> int`
  - `decision_status.pending_participants(meeting) -> list[tuple[User, int, int]]` — `(user, 확인한 공개 항목 수, 전체 공개 항목 수)`, 이름순(`username`), 미완료자만. 진행자 전용.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests.py` 맨 위 import에 `decision_status`를 추가한다: `from . import decision_compiler, decision_status, quiz_status, services, tasks, views, whisper_worker`

`DecisionAckTests` 클래스 뒤에 새 클래스를 추가한다. setUp은 진행자 1명 + 팀원 3명(`dec-alpha`, `dec-beta`, `dec-gamma`), 공개 `Decision` 2개(`d1`, `d2`)와 `undecided` 1개(`d3`)를 만든다.

```python
class DecisionPendingTests(TestCase):
    """결정 확인 미확인 명단(진행자 전용)과 팀 홈 배지(숫자만)."""

    def setUp(self):
        self.host = User.objects.create_user("dechost", password="pw12345!")
        self.alpha = User.objects.create_user("dec-alpha", password="pw12345!")
        self.beta = User.objects.create_user("dec-beta", password="pw12345!")
        self.gamma = User.objects.create_user("dec-gamma", password="pw12345!")
        self.team = Team.objects.create(name="t", created_by=self.host)
        self.team.members.add(self.host, self.alpha, self.beta, self.gamma)
        self.meeting = Meeting.objects.create(
            team=self.team, title="m", host=self.host, record="회의록", transcript="전사본",
        )
        self.d1 = self._decision(order=0, status=Decision.STATUS_PUBLISHED)
        self.d2 = self._decision(order=1, status=Decision.STATUS_PUBLISHED)
        self.d3 = self._decision(order=2, status=Decision.STATUS_UNDECIDED)
        self.client.force_login(self.host)

    def _decision(self, order, status, meeting=None):
        return Decision.objects.create(
            meeting=meeting or self.meeting, order=order,
            spec_text="명세 %d" % order, final_text="확정 %d" % order, status=status,
        )

    def _ack(self, user, decisions, kind=DecisionAgreement.KIND_ACK):
        for decision in decisions:
            DecisionAgreement.objects.update_or_create(
                decision=decision, user=user, defaults={"kind": kind, "comment": ""},
            )

    def _review(self):
        return self.client.get(reverse("decision_review", args=[self.meeting.id]))

    def test_participant_gets_403_on_host_screen(self):
        client = Client()
        client.force_login(self.alpha)
        self.assertEqual(
            client.get(reverse("decision_review", args=[self.meeting.id])).status_code, 403,
        )

    def test_host_is_excluded_from_the_list(self):
        self._ack(self.alpha, [self.d1, self.d2])
        self._ack(self.beta, [self.d1, self.d2])
        self._ack(self.gamma, [self.d1, self.d2])
        # 진행자는 한 항목도 확인하지 않았지만 독려 대상이 아니라서 빠진다.
        self.assertFalse(DecisionAgreement.objects.filter(user=self.host).exists())
        self.assertEqual(decision_status.participant_count(self.meeting), 3)
        review = self._review()
        self.assertEqual(review.context["pending_rows"], [])
        self.assertNotContains(review, "dechost")

    def test_list_shows_name_and_progress_for_unfinished_members(self):
        self._ack(self.alpha, [self.d1, self.d2])
        self._ack(self.beta, [self.d1])
        review = self._review()
        self.assertEqual(review.context["participant_total"], 3)
        self.assertEqual(review.context["participant_done"], 1)
        self.assertEqual(review.context["pending_rows"], [
            {"name": "dec-beta", "acked": 1, "total": 2},
            {"name": "dec-gamma", "acked": 0, "total": 2},
        ])
        self.assertContains(review, "3명 중 1명 확인")
        self.assertContains(review, "1/2개 확인")
        self.assertNotContains(review, "dec-alpha")

    def test_differ_chooser_is_counted_done_and_name_never_appears(self):
        # '달랐어요'도 확인으로 집계된다 — 명단에서 빠지고, 누가 골랐는지는 응답에 없다.
        self._ack(self.alpha, [self.d1, self.d2], kind=DecisionAgreement.KIND_DIFFER)
        review = self._review()
        self.assertNotContains(review, "dec-alpha")
        self.assertEqual(
            [row["name"] for row in review.context["pending_rows"]], ["dec-beta", "dec-gamma"],
        )

    def test_undecided_items_are_not_counted(self):
        # 공개 2개만 분모다. 미결정 d3을 확인하지 않아도 alpha는 완료로 잡힌다.
        self._ack(self.alpha, [self.d1, self.d2])
        self.assertEqual(decision_status.pending_participants(self.meeting), [
            (self.beta, 0, 2), (self.gamma, 0, 2),
        ])

    def test_no_published_items_means_empty_list(self):
        # 확인 기록이 남아 있어도 분모가 0이면 명단과 배지가 모두 비어야 한다(공개 취소 후).
        self._ack(self.alpha, [self.d1, self.d2])
        Decision.objects.filter(meeting=self.meeting).update(status=Decision.STATUS_DRAFT)
        self.assertEqual(decision_status.pending_participants(self.meeting), [])
        review = self._review()
        self.assertFalse(review.context["decision_open"])
        self.assertNotContains(review, "아직 확인하지 않은 팀원")

    def test_revise_puts_that_member_back_to_pending(self):
        self._ack(self.alpha, [self.d1, self.d2])
        self.assertEqual(decision_status.pending_participants(self.meeting), [
            (self.beta, 0, 2), (self.gamma, 0, 2),
        ])
        decision_compiler.revise_final_text(self.d1, "고친 문장")
        pending = decision_status.pending_participants(self.meeting)
        self.assertEqual([(user.username, acked) for user, acked, _ in pending], [
            ("dec-alpha", 1), ("dec-beta", 0), ("dec-gamma", 0),
        ])

    def test_switching_ack_to_differ_keeps_member_done(self):
        self._ack(self.alpha, [self.d1, self.d2])
        self._ack(self.alpha, [self.d1], kind=DecisionAgreement.KIND_DIFFER)
        self.assertNotIn(
            self.alpha, [user for user, _, _ in decision_status.pending_participants(self.meeting)],
        )

    def test_meeting_without_host_counts_every_member(self):
        self.meeting.host = None
        self.meeting.save(update_fields=["host"])
        self.assertEqual(decision_status.participant_count(self.meeting), 4)
        self.assertEqual(len(decision_status.pending_participants(self.meeting)), 4)

    def test_all_confirmed_shows_completion_marker(self):
        for member in (self.alpha, self.beta, self.gamma):
            self._ack(member, [self.d1, self.d2])
        review = self._review()
        self.assertEqual(review.context["participant_pct"], 100)
        self.assertContains(review, "모든 팀원이 결정을 확인했어요")
```

- [ ] **Step 2: 테스트 실행해서 실패 확인**

Run: `cd APL_04 && python manage.py test meetalign.tests.DecisionPendingTests -v 2`
Expected: FAIL — `ImportError: cannot import name 'decision_status'` (또는 `AttributeError`)

- [ ] **Step 3: `decision_status.py` 생성**

`quiz_status.py`를 구조 기준으로 삼아 모듈 docstring(집계 기준·이름 노출 규칙을 한국어로 설명)과 위 Interfaces 블록의 함수들을 구현한다. `quiz_status.py`에서 그대로 가져오는 것: `BADGE_MAX`, `badge_text`, `_member_ids_by_team`, `_target_ids`, `participant_count`.

바뀌는 두 집계 쿼리만 고정한다:

```python
def _published_counts(meeting_ids):
    rows = (
        Decision.objects
        .filter(meeting_id__in=meeting_ids, status=Decision.STATUS_PUBLISHED)
        .values("meeting_id")
        .annotate(n=Count("id"))
    )
    return {row["meeting_id"]: row["n"] for row in rows}


def _acked_counts(meeting_ids):
    rows = (
        DecisionAgreement.objects
        .filter(
            decision__meeting_id__in=meeting_ids,
            decision__status=Decision.STATUS_PUBLISHED,
        )
        .values("decision__meeting_id", "user_id")
        .annotate(n=Count("id"))
    )
    return {(row["decision__meeting_id"], row["user_id"]): row["n"] for row in rows}
```

`pending_participants(meeting)`는 `quiz_status.pending_participants`와 같은 형태 — 공개 항목 수가 0이면 `[]`, 아니면 대상 팀원을 `username` 순으로 돌며 `확인 수 < 전체`인 사람만 `(user, 확인 수, 전체)`로 모은다. `kind`는 조회하지 않는다.

- [ ] **Step 4: `decision_review` 뷰에 명단 집계 추가**

`views.py:15` import에 `decision_status`를 넣는다. `decision_review`에서 `rows` 루프가 끝나고 `warned`/`drafts`/`blockers`를 계산한 뒤, `return render(...)` 바로 앞에 집계를 넣는다(권한 검사는 함수 앞머리의 `_require_host(meeting, request.user)`가 이미 통과시킨 상태다):

```python
# 미확인 팀원 이름은 이 화면(진행자 전용)에서만 모은다. 위 _require_host를 통과한
# 뒤에만 조회하므로, 팀원 요청에는 이름이 담긴 HTML이 애초에 만들어지지 않는다.
decision_open = any(row["decision"].status == Decision.STATUS_PUBLISHED for row in rows)
pending = decision_status.pending_participants(meeting) if decision_open else []
participant_total = decision_status.participant_count(meeting) if decision_open else 0
participant_done = participant_total - len(pending)
```

컨텍스트 키: `decision_open`, `pending_rows`(`[{"name": user.username, "acked": acked, "total": total} ...]`), `participant_total`, `participant_done`, `participant_pct`(`round(done * 100 / total) if total else 0`).

- [ ] **Step 5: `decision_review.html`에 명단 카드 추가**

`question_review.html:28-56`의 카드를 그대로 미러링한다(같은 `card elevated` / `progress-row` / `progress-track` / `progress-fill` / `plain` / `item` 마크업과 같은 사람 아이콘 SVG). 위치는 compiled 분기 메인 카드의 닫는 `</div>` 뒤, 파일 끝 `{% endif %}` 앞. `{% if decision_open %}`으로 감싼다.

문구만 바꾼다: 제목 `아직 확인하지 않은 팀원`, 진행 표시 `{{ participant_total }}명 중 {{ participant_done }}명 확인`, 각 행 meta `{{ row.acked }}/{{ row.total }}개 확인`, 전원 완료 `모든 팀원이 결정을 확인했어요`, 하단 안내 `독려용 명단입니다. 누가 '제 이해와 달랐어요'를 골랐는지는 이 화면에서도 보이지 않습니다.`

- [ ] **Step 6: 테스트 통과 확인**

Run: `cd APL_04 && python manage.py test meetalign.tests.DecisionPendingTests -v 2`
Expected: PASS (10 tests)

- [ ] **Step 7: 커밋**

```bash
git add APL_04/meetalign/decision_status.py APL_04/meetalign/views.py APL_04/meetalign/templates/meetalign/decision_review.html APL_04/meetalign/tests.py
git commit -m "$(cat <<'EOF'
결정 확인: 진행자 화면에 아직 확인하지 않은 팀원 명단 추가

공개 항목 전부에 DecisionAgreement가 있는 팀원을 완료로 보고, 미완료자만 이름과
진척도로 보여준다. 진행자 본인과 미결정 항목은 집계에서 뺀다. 이름이 담긴 조회는
_require_host 통과 뒤에만 하므로 팀원 요청에는 명단이 담긴 HTML이 생성되지 않고,
'달랐어요'를 고른 사람은 완료로 잡혀 명단에 나오지 않는다.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: 회의 목록 배지를 결정 확인 기준으로 전환

**Files:**
- Modify: `APL_04/meetalign/views.py` (`_meeting_rows`, 줄 94-112)
- Modify: `APL_04/meetalign/templates/meetalign/meeting_list.html` (줄 33-38)
- Test: `APL_04/meetalign/tests.py` (`DecisionPendingTests`에 배지 테스트 추가, `QuizPendingTests`의 팀 홈 어서션 조정)

**Interfaces:**
- Consumes: Task 1의 `decision_status.badge_text`, `decision_status._published_counts`, `decision_status._acked_counts`, `decision_status._member_ids_by_team`, `decision_status._target_ids`
- Produces: `decision_status.pending_count_for_meetings(meetings) -> dict[int, int | None]` — 공개 항목이 없는 회의는 `None`. `views._meeting_rows`가 돌려주는 row의 키는 `meeting`, `decision_open`, `pending`, `badge`, `tooltip`.

- [ ] **Step 1: 실패하는 테스트 작성**

`DecisionPendingTests`에 팀 홈 헬퍼와 배지 테스트를 추가한다.

```python
    def _home(self, user=None):
        client = self.client
        if user is not None:
            client = Client()
            client.force_login(user)
        return client.get(reverse("meeting_list", args=[self.team.id]))

    def test_badge_counts_members_who_have_not_confirmed(self):
        self._ack(self.alpha, [self.d1, self.d2])
        home = self._home()
        self.assertEqual(home.context["rows"][0]["pending"], 2)
        self.assertContains(home, 'class="pending-badge"')
        self.assertContains(home, ">2</span>")
        self.assertContains(home, "아직 2명이 결정을 확인하지 않았어요")

    def test_unpublished_meeting_shows_no_badge(self):
        Decision.objects.filter(meeting=self.meeting).update(status=Decision.STATUS_DRAFT)
        home = self._home()
        self.assertIsNone(home.context["rows"][0]["pending"])
        self.assertNotContains(home, 'class="pending-badge"')
        self.assertNotContains(home, "모두 확인")

    def test_meeting_with_only_undecided_items_shows_no_badge(self):
        Decision.objects.filter(meeting=self.meeting).update(status=Decision.STATUS_UNDECIDED)
        self.assertIsNone(self._home().context["rows"][0]["pending"])

    def test_all_confirmed_shows_clear_marker(self):
        for member in (self.alpha, self.beta, self.gamma):
            self._ack(member, [self.d1, self.d2])
        home = self._home()
        self.assertEqual(home.context["rows"][0]["pending"], 0)
        self.assertNotContains(home, 'class="pending-badge"')
        self.assertContains(home, "✓ 모두 확인")

    def test_badge_shows_99_plus_over_limit(self):
        self.assertEqual(decision_status.badge_text(99), "99")
        self.assertEqual(decision_status.badge_text(100), "99+")

    def test_team_with_host_only_shows_clear_marker(self):
        self.team.members.remove(self.alpha, self.beta, self.gamma)
        home = self._home()
        self.assertEqual(home.context["rows"][0]["pending"], 0)
        self.assertContains(home, "✓ 모두 확인")

    def test_other_meetings_agreements_are_not_mixed_in(self):
        other = Meeting.objects.create(team=self.team, title="other", host=self.host, transcript="전사본")
        other_decision = self._decision(order=0, status=Decision.STATUS_PUBLISHED, meeting=other)
        self._ack(self.alpha, [other_decision])
        counts = decision_status.pending_count_for_meetings([self.meeting, other])
        self.assertEqual(counts[self.meeting.id], 3)
        self.assertEqual(counts[other.id], 2)

    def test_member_pages_contain_no_other_member_names(self):
        self._ack(self.alpha, [self.d1])
        client = Client()
        client.force_login(self.alpha)
        for url in (
            reverse("meeting_list", args=[self.team.id]),
            reverse("meeting_detail", args=[self.meeting.id]),
            reverse("decision_ack", args=[self.meeting.id]),
        ):
            response = client.get(url)
            self.assertEqual(response.status_code, 200)
            body = response.content.decode()
            for name in ("dec-beta", "dec-gamma", "dechost"):
                self.assertNotIn(name, body, "%s 응답에 %s가 노출됨" % (url, name))

    def test_meeting_list_query_count_does_not_grow_with_meetings(self):
        self._ack(self.alpha, [self.d1])
        url = reverse("meeting_list", args=[self.team.id])
        with CaptureQueriesContext(connection) as one_meeting:
            self.assertEqual(self.client.get(url).status_code, 200)
        for i in range(4):
            extra = Meeting.objects.create(team=self.team, title="m%d" % i, host=self.host, transcript="전사본")
            self._decision(order=0, status=Decision.STATUS_PUBLISHED, meeting=extra)
        with CaptureQueriesContext(connection) as five_meetings:
            response = self.client.get(url)
        self.assertEqual(len(response.context["rows"]), 5)
        self.assertEqual(len(five_meetings.captured_queries), len(one_meeting.captured_queries))
```

- [ ] **Step 2: 테스트 실행해서 실패 확인**

Run: `cd APL_04 && python manage.py test meetalign.tests.DecisionPendingTests -v 2`
Expected: Task 1 테스트는 PASS, 새 배지 테스트 9개는 FAIL — `AttributeError: module 'meetalign.decision_status' has no attribute 'pending_count_for_meetings'` 및 `rows[0]["pending"]`이 퀴즈 기준이라 값이 맞지 않음

- [ ] **Step 3: `decision_status.pending_count_for_meetings` 구현**

`quiz_status.pending_count_for_meetings`와 같은 흐름 — 회의 목록이 비면 `{}`, 아니면 `_published_counts`/`_acked_counts`/`_member_ids_by_team` 세 번의 집계 쿼리만 쓰고 회의마다 `total`이 0이면 `None`, 아니면 `len(targets) - 완료 수`를 담는다. 완료 판정은 `_acked_counts.get((meeting.id, user_id), 0) >= total`.

- [ ] **Step 4: `_meeting_rows`를 결정 확인 기준으로 전환**

`views.py:94` `_meeting_rows`의 docstring을 결정 확인 기준으로 고치고, `quiz_status.pending_count_for_meetings` → `decision_status.pending_count_for_meetings`, `quiz_status.badge_text` → `decision_status.badge_text`로 바꾼다. row 키 `quiz_open` → `decision_open`, 툴팁 문자열 → `"아직 %d명이 결정을 확인하지 않았어요" % pending`. `views.py`는 `quiz_status`도 계속 import한다(`question_review`가 쓴다).

- [ ] **Step 5: `meeting_list.html` 문구·키 갱신**

줄 33의 `{% if row.quiz_open %}` → `{% if row.decision_open %}`, 줄 37의 `<span class="pending-clear" title="모두 확인 퀴즈를 마쳤어요">✓ 모두 완료</span>` → `title="모두 결정을 확인했어요"`와 본문 `✓ 모두 확인`. `pending-badge` 줄은 그대로.

- [ ] **Step 6: `QuizPendingTests`의 팀 홈 어서션 조정 (테스트 삭제 금지)**

`QuizPendingTests`는 이제 `question_review`(퀴즈)만 검증한다. `self._home()`을 쓰는 다섯 테스트에서 팀 홈 어서션을 `quiz_status` 직접 호출로 바꾼다 — 퀴즈 집계 로직 커버리지는 유지하면서 화면 의존만 끊는다. 헬퍼를 클래스에 추가한다:

```python
    def _quiz_pending(self):
        return quiz_status.pending_count_for_meetings([self.meeting])[self.meeting.id]
```

- `test_unpublished_quiz_shows_no_badge_and_no_host_section` → 이름을 `test_unpublished_quiz_shows_no_host_section`으로 바꾸고, `home` 3줄을 `self.assertIsNone(self._quiz_pending())` 한 줄로 대체.
- `test_badge_counts_participants_who_have_not_finished` → 이름을 `test_review_lists_participants_who_have_not_finished`으로 바꾸고, `home` 블록(4줄)을 `self.assertEqual(self._quiz_pending(), 2)`로 대체.
- `test_all_finished_shows_completion_markers` → `home` 블록(4줄)을 `self.assertEqual(self._quiz_pending(), 0)`으로 대체.
- `test_host_is_excluded_from_the_count` → `self._home().context["rows"][0]["pending"]` → `self._quiz_pending()`.
- `test_newly_published_question_makes_finished_participant_pending_again` → `self._home().context["rows"][0]["pending"]` 세 곳 → `self._quiz_pending()`.
- `test_participant_pages_contain_no_other_participant_names`, `test_participant_gets_403_on_host_screen`, `test_host_screen_does_not_reveal_who_chose_what`, `test_badge_shows_99_plus_over_limit`, `test_meeting_list_query_count_does_not_grow_with_meetings` → 수정하지 않는다(결정 기준으로 바뀐 뒤에도 그대로 통과한다).

테스트를 추가·삭제하지 않으므로 `QuizPendingTests`의 개수는 그대로다.

- [ ] **Step 7: 전체 테스트 실행**

Run: `cd APL_04 && python manage.py test -v 1`
Expected: PASS, 총 개수 = 344 + Task 1·2 신규 개수. 실패·에러 0

- [ ] **Step 8: 커밋**

```bash
git add APL_04/meetalign/decision_status.py APL_04/meetalign/views.py APL_04/meetalign/templates/meetalign/meeting_list.html APL_04/meetalign/tests.py
git commit -m "$(cat <<'EOF'
회의 목록 배지를 퀴즈 기준에서 결정 확인 기준으로 전환

decision_status.pending_count_for_meetings()로 아직 결정을 확인하지 않은 팀원 수만
세어 배지에 담는다. 공개 항목이 없는 회의는 배지를 숨기고, 전원 확인하면 "모두 확인"을
보여준다. 회의 개수와 무관하게 집계 쿼리 세 번으로 처리한다. quiz_status는 아직
question_review가 쓰므로 남겨 두고 팀 홈 참조만 끊었다.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: 작업 요약 기록

**Files:**
- Modify: `진행사항.txt` (기존 결정 확인 단락 뒤, "알려진 이슈" 앞)

- [ ] **Step 1: 요약 추가**

기존 파일의 문체(`- ` 불릿, 평서문, 기준과 이유를 함께 적는 방식)를 따라 이번 작업을 적는다. 담을 내용: 진행자 화면 미확인 명단의 완료 기준(공개 항목 전부 확인, `kind` 무관, 진행자·`undecided` 제외)과 `_require_host` 뒤에서만 조회한다는 점, 팀 홈 배지를 결정 확인 기준으로 바꾼 것과 `quiz_status`를 남겨 둔 이유, 모델 변경·마이그레이션이 없다는 점, 전체 테스트 개수와 통과 결과(Step 2에서 실제로 확인한 숫자).

- [ ] **Step 2: 전체 테스트 재실행해서 숫자 확정**

Run: `cd APL_04 && python manage.py test -v 1`
Expected: `Ran N tests` / `OK` — 이 N을 `진행사항.txt`에 적고, "기존 344개 / 신규 N-344개"로 보고한다.

- [ ] **Step 3: 커밋**

```bash
git add 진행사항.txt
git commit -m "$(cat <<'EOF'
진행사항: 결정 확인 미확인 명단과 회의 목록 배지 전환 요약 추가

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## 완료 조건

- `python manage.py test` 전체 통과, 실패·에러 0
- 테스트 총 개수 ≥ 344 + 신규분 (기존 344개가 줄지 않음)
- 새 마이그레이션 파일 0개 (`git status`로 확인)
- `services.py` 변경 없음, `meetalign/services/` 생성 없음 (`git status`로 확인)
- 커밋 3개(명단 / 배지 / 문서), `push` 하지 않음
