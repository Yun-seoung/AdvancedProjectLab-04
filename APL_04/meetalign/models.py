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
# 기존 모델은 건드리지 않는다. 회의 원문에서 '실행 명세'를 뽑아 모호한 항목만 블라인드로
# 동시 선택하게 하고, 갈리면 진행자가 한 줄로 확정한다.
# ---------------------------------------------------------------------------


class Decision(models.Model):
    """회의 원문에서 추출한 결정 하나를 실행 명세로 옮긴 것.

    [누가 · 무엇을 · 언제까지 · 완료 기준 · 확정 여부]가 한 줄의 명세가 되고, 빈칸이나
    모호어가 있으면 warnings에 경고 사유가 쌓인다(회의당 최대 5개까지만 만든다).
    status는 진행자 검토(draft) → 투표 중(voting) → 전원 일치면 확정(resolved),
    갈리면 진행자 확정 대기(pending_host), 진행자가 '아직 안 정해진 것 같음'을 골랐으면
    미결정(undecided)으로 흐른다.
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
    STATUS_VOTING = "voting"
    STATUS_PENDING_HOST = "pending_host"
    STATUS_RESOLVED = "resolved"
    STATUS_UNDECIDED = "undecided"
    STATUS_CHOICES = [
        (STATUS_DRAFT, "진행자 검토 중"),
        (STATUS_VOTING, "투표 중"),
        (STATUS_PENDING_HOST, "진행자 확정 대기"),
        (STATUS_RESOLVED, "확정"),
        (STATUS_UNDECIDED, "미결정"),
    ]

    meeting = models.ForeignKey(Meeting, on_delete=models.CASCADE, related_name="decisions")
    order = models.PositiveSmallIntegerField(default=0)
    who = models.CharField(max_length=200, blank=True, default="")
    what = models.TextField(blank=True, default="")
    due_text = models.CharField(max_length=200, blank=True, default="")
    done_criteria = models.TextField(blank=True, default="")
    confirm_state = models.CharField(max_length=12, choices=CONFIRM_CHOICES, default=CONFIRM_UNSET)
    # 컴파일 경고 사유 문자열 목록. 예: ["담당자 빈칸", "모호어 '쯤'", "같은 대상에 다른 날짜"].
    warnings = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=14, choices=STATUS_CHOICES, default=STATUS_DRAFT)
    # 갈렸을 때 진행자가 한 줄로 쓴 확정 문장. 전원 일치로 자동 발행된 경우에도 그 해석 문장을 넣는다.
    final_text = models.TextField(blank=True, default="")
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


class Interpretation(models.Model):
    """경고가 난 Decision 하나에 대해 AI가 '가상 청자'로서 만든 서로 다른 해석 후보.

    evidence는 전사 원문에 그대로 등장하는 문장이어야 하며, 지어낸 근거가 붙은 해석은
    저장 단계에서 버린다(기존 CheckQuestion.evidence 검증과 같은 방식).
    정답은 없다 — 어느 해석이 맞는지는 팀원들의 선택으로 드러난다.
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
        """화면에 쓰는 '해석 A/B/C' 꼬리표."""
        return chr(ord("A") + self.order) if self.order < 26 else str(self.order + 1)

    def __str__(self):
        return "해석 %s: %s" % (self.label, self.text[:30])


class DecisionVote(models.Model):
    """항목별 블라인드 선택. (항목, 사용자) 쌍으로 유일해서 중복 투표가 막힌다.

    user를 저장하는 목적은 중복 투표 방지와 '내가 이미 골랐는지' 판정뿐이다. 어떤 화면에도
    누가 무엇을 골랐는지는 내보내지 않고, 인원 수 집계만 보여준다(진행자에게도 마찬가지).
    """

    KIND_INTERPRETATION = "interpretation"
    KIND_CUSTOM = "custom"
    KIND_NOT_DECIDED = "not_decided"
    KIND_CHOICES = [
        (KIND_INTERPRETATION, "해석 선택"),
        (KIND_CUSTOM, "직접 쓰기"),
        (KIND_NOT_DECIDED, "아직 안 정해진 것 같음"),
    ]

    decision = models.ForeignKey(Decision, on_delete=models.CASCADE, related_name="votes")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="decision_votes")
    kind = models.CharField(max_length=16, choices=KIND_CHOICES)
    # kind가 interpretation일 때만 채워진다. 해석이 지워지면 표는 '직접 쓰기'가 아니라 그냥 끊긴다.
    interpretation = models.ForeignKey(Interpretation, on_delete=models.SET_NULL, null=True, blank=True, related_name="votes")
    custom_text = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("decision", "user")

    def __str__(self):
        return "%s - %s" % (self.decision_id, self.kind)


class DecisionAgreement(models.Model):
    """갈린 항목을 진행자가 한 줄로 확정한 뒤, 팀원이 "이 내용으로 맞춤"을 탭한 기록.
    같은 사람이 여러 번 눌러도 한 건만 남는다."""

    decision = models.ForeignKey(Decision, on_delete=models.CASCADE, related_name="agreements")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="decision_agreements")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("decision", "user")

    def __str__(self):
        return "%s 동의 (%s)" % (self.decision_id, self.user)


class Checkpoint(models.Model):
    """착수 체크포인트(Phase E). 확정된 명세에서 본인이 담당자인 항목에 "완료 모습 한 줄"을
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
