import os
import secrets
import string
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

# 혼동하기 쉬운 문자(0/O, 1/I)는 빼고 초대 코드를 만든다.
INVITE_CODE_ALPHABET = "".join(c for c in string.ascii_uppercase + string.digits if c not in "0O1I")


def _generate_invite_code():
    return "".join(secrets.choice(INVITE_CODE_ALPHABET) for _ in range(6))


class Team(models.Model):
    name = models.CharField(max_length=100, unique=True)
    invite_code = models.CharField(max_length=8, unique=True, editable=False, default=_generate_invite_code)
    members = models.ManyToManyField(settings.AUTH_USER_MODEL, related_name="teams")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="created_teams")

    def __str__(self):
        return self.name


class Meeting(models.Model):
    STATUS_PROCESSING = "processing"
    STATUS_DONE = "done"
    STATUS_FAILED = "failed"

    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="meetings")
    title = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)
    ended = models.BooleanField(default=False)
    deadline = models.DateField(null=True, blank=True)
    host = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="hosted_meetings")
    # 회의록(전사+요약). 지금은 저장 자리만 있고, 채우는 건 STT/LLM 작업에서 한다.
    record = models.TextField(blank=True)
    transcript = models.TextField(blank=True)
    processing_status = models.CharField(max_length=20, blank=True, default="")
    # 이해도 검증 질문을 마지막으로 생성했을 때의 전사본 스냅샷. 그 뒤로 녹음이
    # 추가되어 transcript가 길어지면(이 값과 달라지면) "추가 질문 생성"을 제안할 수 있다.
    check_questions_transcript = models.TextField(blank=True, default="")

    def __str__(self):
        return self.title

    @property
    def is_due_tomorrow(self):
        return self.deadline is not None and self.deadline - timezone.localdate() == timedelta(days=1)


class Recording(models.Model):
    STATUS_PROCESSING = "processing"
    STATUS_DONE = "done"
    STATUS_FAILED = "failed"

    meeting = models.ForeignKey(Meeting, on_delete=models.CASCADE, related_name="recordings")
    file = models.FileField(upload_to="recordings/")
    transcript = models.TextField(blank=True)
    status = models.CharField(max_length=20, default=STATUS_PROCESSING)
    error_message = models.CharField(max_length=300, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]

    @property
    def filename(self):
        return os.path.basename(self.file.name)

    def __str__(self):
        return self.filename


class SttUsage(models.Model):
    """외부 STT(CLOVA Speech)로 전사한 음성 길이(초) 기록. 이번 달 무료 사용량이 남았는지 판단하는 데 쓴다.
    녹음을 지워도 그 달에 쓴 양은 그대로 남아야 하므로 recording이 사라지면 비워 둔다(SET_NULL)."""
    recording = models.ForeignKey(Recording, on_delete=models.SET_NULL, null=True, blank=True, related_name="stt_usages")
    audio_seconds = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return "%d초 (%s)" % (self.audio_seconds, self.created_at)


class ChatMessage(models.Model):
    """회의 내용에 대해 질문하는 대화창의 기록. 사용자별로 따로 저장되어
    다음 질문을 할 때 이전 대화를 참고할 수 있게 한다. 익명 질문함(Question)과는 별개다."""
    ROLE_USER = "user"
    ROLE_ASSISTANT = "assistant"

    meeting = models.ForeignKey(Meeting, on_delete=models.CASCADE, related_name="chat_messages")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="chat_messages")
    role = models.CharField(max_length=10, choices=[(ROLE_USER, "user"), (ROLE_ASSISTANT, "assistant")])
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return "%s(%s): %s" % (self.user, self.role, self.text[:30])


class Question(models.Model):
    """회의별 대화방의 익명 질문. 주최자와 다른 팀원에게는 작성자를 보여주지 않지만,
    답변을 본인만 볼 수 있도록 내부적으로는 작성자를 저장한다."""
    meeting = models.ForeignKey(Meeting, on_delete=models.CASCADE, related_name="questions")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="questions")
    text = models.TextField()
    answer = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.text[:30]


class CheckQuestion(models.Model):
    """이해도 검증을 위해 (LLM이) 낸 객관식 질문. 회의당 여러 개이며 팀원 전원이 각자 답한다.
    주최자가 검토/수정 후 공개(published)해야 팀원에게 보인다."""
    STATUS_DRAFT = "draft"
    STATUS_PUBLISHED = "published"

    meeting = models.ForeignKey(Meeting, on_delete=models.CASCADE, related_name="check_questions")
    text = models.TextField()
    choices = models.JSONField(default=list)
    correct_choice = models.PositiveSmallIntegerField(default=0)
    evidence = models.TextField(blank=True, default="")
    status = models.CharField(max_length=10, choices=[(STATUS_DRAFT, "draft"), (STATUS_PUBLISHED, "published")], default=STATUS_DRAFT)
    order = models.PositiveSmallIntegerField(default=0)
    # 몇 번째로 생성된 질문 묶음인지(0 = 최초 생성분, 1 = 녹음 추가 후 첫 추가분, ...).
    # 팀원 화면에서 묶음별로 나눠서 보여주는 데 쓴다.
    batch = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["batch", "order", "id"]
        constraints = [models.UniqueConstraint(fields=["meeting", "batch", "order"], name="unique_check_question_batch_order")]

    def __str__(self):
        return self.text[:30]


class Answer(models.Model):
    """CheckQuestion에 대한 팀원별 답변(선택한 보기 번호). 같은 질문이라도 사람마다 따로
    저장되어야 "누구와 누구가 다르게 이해했는지" 비교가 가능하다."""
    question = models.ForeignKey(CheckQuestion, on_delete=models.CASCADE, related_name="answers")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="check_answers")
    selected_choice = models.PositiveSmallIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("question", "user")

    @property
    def is_correct(self):
        return self.selected_choice == self.question.correct_choice

    def __str__(self):
        return "%s - %s" % (self.user, self.question_id)




# ---------------------------------------------------------------------------
# 결정 컴파일러 (실험 기능). 기존 CheckQuestion/Answer 흐름과 완전히 분리된 새 테이블이며,
# 기존 모델은 건드리지 않는다.
#
# 흐름: 회의 원문에서 '실행 명세'를 뽑고(①), 모호한 항목마다 해석 후보를 만들어(②)
# 진행자에게만 참고로 보여준다. 진행자가 항목마다 확정하거나 '아직 미정'으로 넘기고(③),
# "확정 명세 공개"를 누르면 팀원 전원에게 보인다(④). 팀원은 항목마다 확인했는지 또는
# 자기 이해와 달랐는지를 남긴다(⑤).
#
# ※ 블라인드 투표 모드(보류): 팀원 전원이 해석을 블라인드로 동시 선택하고 집계로 판정하는
#    방식을 먼저 만들었다가(DecisionVote 모델 + 전원일치/갈림/미결정 판정) 진행자 확정
#    모드로 방향을 바꿨다. 되살릴 때 참고할 구현은 커밋 7eb074a(블라인드 선택 화면)과
#    ac6fb89(분기 판정)에 남아 있다. 다시 실험한다면 DecisionVote(decision, user,
#    kind=interpretation|custom|not_decided, interpretation, custom_text,
#    unique(decision,user))를 새 마이그레이션으로 추가하면 된다 — 아래 모델들은 그대로 둔
#    채 옆에 붙일 수 있는 구조다.
# ---------------------------------------------------------------------------


class Decision(models.Model):
    """회의 원문에서 추출한 결정 하나를 실행 명세로 옮긴 것.

    [누가 · 무엇을 · 언제까지 · 완료 기준 · 확정 여부]가 한 줄의 명세가 되고, 빈칸이나
    모호어가 있으면 warnings에 경고 사유가 쌓인다(회의당 최대 5개까지만 만든다).

    status는 draft(진행자 검토 중) → published(확정 명세 공개) 로 가고, 진행자가 '아직
    미정'으로 넘긴 항목만 undecided가 되어 다음 회의로 이월된다.
    """

    CONFIRM_CONFIRMED = "confirmed"
    CONFIRM_DISCUSSING = "discussing"
    CONFIRM_UNSET = "unset"
    CONFIRM_CHOICES = [
        (CONFIRM_CONFIRMED, "확정"),
        (CONFIRM_DISCUSSING, "논의중"),
        (CONFIRM_UNSET, "미정"),
    ]

    STATUS_DRAFT = "draft"
    STATUS_PUBLISHED = "published"
    STATUS_UNDECIDED = "undecided"
    STATUS_CHOICES = [
        (STATUS_DRAFT, "진행자 검토 중"),
        (STATUS_PUBLISHED, "공개됨"),
        (STATUS_UNDECIDED, "미결정"),
    ]

    meeting = models.ForeignKey(Meeting, on_delete=models.CASCADE, related_name="decisions")
    order = models.PositiveSmallIntegerField(default=0)
    who = models.CharField(max_length=200, blank=True, default="")
    what = models.TextField(blank=True, default="")
    due_text = models.CharField(max_length=200, blank=True, default="")
    done_criteria = models.TextField(blank=True, default="")
    confirm_state = models.CharField(max_length=12, choices=CONFIRM_CHOICES, default=CONFIRM_UNSET)
    # 컴파일 경고 사유 문자열 목록. 예: ["담당자 빈칸", "모호어 '쯤'", "같은 항목에 다른 날짜"].
    warnings = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_DRAFT)
    # 진행자가 확정한 한 줄. 해석 후보에서 고른 문장이거나 직접 쓴 문장이다.
    # 경고가 없는 항목은 명세 자체가 확정 내용이라 비어 있을 수 있다.
    final_text = models.TextField(blank=True, default="")
    # 진행자가 '아직 미정'으로 넘긴 항목. 공개 시점에 status=undecided가 된다.
    marked_undecided = models.BooleanField(default=False)
    # 공개 뒤에 확정 문장이 수정된 항목. 팀원 화면에 "내용이 수정되었어요"를 띄우는 데 쓴다.
    # 수정 시 그 항목의 확인 기록(DecisionAgreement)은 모두 지워지므로, 다시 확인한 사람에게는
    # 안내가 사라진다(안내는 '내 확인 기록이 없을 때'만 뜬다).
    revised = models.BooleanField(default=False)
    # 미결정으로 이월된 안건을 진행자가 "처리됨"으로 닫은 시각. null이면 이후 회의 화면에 계속 보인다.
    carryover_resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["order", "id"]
        constraints = [models.UniqueConstraint(fields=["meeting", "order"], name="unique_decision_order")]

    def __str__(self):
        return "%s / %s" % (self.who or "?", (self.what or "")[:30])

    @property
    def has_warning(self):
        return bool(self.warnings)

    @property
    def spec_line(self):
        """화면과 프롬프트에서 같이 쓰는 한 줄 명세. 빈칸은 '?'로 드러낸다."""
        return "%s · %s · %s · %s · %s" % (
            self.who or "?", self.what or "?", self.due_text or "?",
            self.done_criteria or "?", self.get_confirm_state_display(),
        )

    @property
    def is_settled(self):
        """진행자가 이 항목을 처리했는가 — 확정 문장을 썼거나 '아직 미정'으로 넘겼거나.

        경고가 없는 항목은 고를 것이 없으므로 처리된 것으로 본다. 경고 항목 중 하나라도
        처리되지 않으면 "확정 명세 공개"를 막는다.
        """
        return bool(self.final_text) or self.marked_undecided or not self.warnings

    @property
    def published_text(self):
        """팀원에게 보여줄 확정 내용. 진행자가 쓴 한 줄이 있으면 그것, 없으면 명세 한 줄."""
        return self.final_text or self.spec_line


class Interpretation(models.Model):
    """경고가 붙은 Decision 하나에 대해 AI가 '가상 청자'로서 만든 서로 다른 해석 후보.

    evidence는 전사 원문에 그대로 등장하는 문장이어야 하며, 지어낸 근거가 붙은 해석은
    저장 단계에서 버린다(기존 CheckQuestion.evidence 검증과 같은 방식).
    정답은 없다 — 진행자가 참고해서 고르거나 직접 쓰는 데 쓴다. 팀원에게는 보여주지 않는다.
    """

    decision = models.ForeignKey(Decision, on_delete=models.CASCADE, related_name="interpretations")
    order = models.PositiveSmallIntegerField(default=0)
    text = models.TextField()
    evidence = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["order", "id"]
        constraints = [models.UniqueConstraint(fields=["decision", "order"], name="unique_interpretation_order")]

    @property
    def label(self):
        """화면에 쓰는 '후보 A/B/C' 꼬리표."""
        return chr(ord("A") + self.order) if self.order < 26 else str(self.order + 1)

    def __str__(self):
        return "후보 %s: %s" % (self.label, self.text[:30])


class DecisionAgreement(models.Model):
    """공개된 확정 명세에 대한 팀원별 확인 기록.

    kind=ack는 "확인했어요", kind=differ는 "제 이해와 달랐어요"다. comment는 differ에
    덧붙이는 한 줄 의견(선택)이다.

    (항목, 사용자) 쌍으로 유일하고 기존 Answer처럼 수정할 수 있다 — 진행자 집계는 항상
    현재 선택 기준이다. user를 저장하는 목적은 중복 방지와 본인 화면의 '내 선택' 표시뿐이고,
    누가 differ를 골랐는지는 어떤 화면에도 내보내지 않는다.
    한 줄 의견도 작성자를 비공개로 묶어서 보여준다.
    """

    KIND_ACK = "ack"
    KIND_DIFFER = "differ"
    KIND_CHOICES = [
        (KIND_ACK, "확인했어요"),
        (KIND_DIFFER, "제 이해와 달랐어요"),
    ]

    decision = models.ForeignKey(Decision, on_delete=models.CASCADE, related_name="agreements")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="decision_agreements")
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default=KIND_ACK)
    comment = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("decision", "user")

    @property
    def differs(self):
        return self.kind == self.KIND_DIFFER

    def __str__(self):
        return "%s - %s" % (self.decision_id, self.kind)


class Checkpoint(models.Model):
    """착수 체크포인트(Phase E). 공개된 확정 명세에서 본인이 담당자인 항목에 "완료 모습 한 줄"을
    쓰면, AI가 확정 명세와 대조해 '맞음 / 차이 있음(이유 한 줄)'을 돌려준다."""

    VERDICT_MATCH = "match"
    VERDICT_DIFF = "diff"
    VERDICT_UNKNOWN = "unknown"
    VERDICT_CHOICES = [
        (VERDICT_MATCH, "맞음"),
        (VERDICT_DIFF, "차이 있음"),
        (VERDICT_UNKNOWN, "판정 실패"),
    ]

    decision = models.ForeignKey(Decision, on_delete=models.CASCADE, related_name="checkpoints")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="decision_checkpoints")
    expected_text = models.TextField()
    verdict = models.CharField(max_length=10, choices=VERDICT_CHOICES, default=VERDICT_UNKNOWN)
    reason = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("decision", "user")

    def __str__(self):
        return "%s - %s" % (self.decision_id, self.verdict)
