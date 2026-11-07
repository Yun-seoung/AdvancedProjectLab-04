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


# ---------------------------------------------------------------------------
# 결정 사항 추출 기능.
#
# 흐름: 회의 원문에서 '결정 사항'를 뽑고(①), 모호한 항목마다 해석 후보를 만들어(②)
# 진행자에게만 참고로 보여준다. 진행자가 항목마다 확정하거나 '아직 미정'으로 넘기고(③),
# "확정된 결정 공개"를 누르면 팀원 전원에게 보인다(④). 팀원은 항목마다 확인했는지 또는
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
    """회의 원문에서 뽑은 결정 하나와, 그대로 공개될 확정 문장.

    what은 안건 한 줄, final_text는 팀원에게 그대로 공개되는 문장이다. who·due_text·
    done_criteria·confirm_state는 이번 재설계 전 레코드를 보존하려고 남겨 둔 칸이고 신규
    추출에서는 채우지 않는다 — 공개 기준이 둘이 되지 않게 하려는 것이다.

    AI가 다르게 이해될 수 있다고 본 항목은 divergence_reason이 차고, 규칙 안전망이 걸린
    항목은 warnings가 찬다. 둘 중 하나라도 있으면(has_warning) 진행자가 확인해야 공개된다.

    status는 draft(진행자 검토 중) → published(확정 문장 공개) 로 가고, 진행자가 '다음 회의로
    넘김'을 고른 항목만 undecided가 되어 다음 회의로 이월된다.
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
    # 규칙 안전망이 붙인 사유 문자열 목록.
    # 예: ["확정 문장에 모호한 표현이 있어요: '나중에'", "원문에 다른 날짜가 섞여 있어요: 금요일, 월요일"].
    warnings = models.JSONField(default=list, blank=True)
    # AI가 쓴 "왜 다르게 이해할 수 있나" 한 줄. 비어 있으면 AI가 명확하다고 본 결정이다.
    # 규칙 사유(warnings)와 나란히 두는 이유: 화면에서 AI 이유를 앞에 굵게, 규칙 사유를
    # 그 아래 작게 보여주기 때문이다. 둘 중 하나라도 있으면 진행자 확인 대상이다(has_warning).
    divergence_reason = models.TextField(blank=True, default="")
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_DRAFT)
    # 그대로 공개되는 확정 문장. 공개 경로는 이 칸 하나뿐이다(published_text).
    # 확인이 필요 없던 항목은 AI가 쓴 문장, 확인이 필요했던 항목은 진행자가 고르거나 직접 쓴
    # 문장, '다음 회의로 넘김'은 undecided_sentence()가 만든 문장이 들어온다. 문장이 비어 있는
    # 항목은 공개할 수 없다(publish_blockers가 막는다).
    final_text = models.TextField(blank=True, default="")
    # AI가 쓴 후보 중 모호어·지시어가 남아 선택지에서는 빠졌지만("_parse_candidates"),
    # 그 부분만 빈칸 표시로 바꾸면 '직접 쓰기'의 출발점으로 쓸 만한 문장. 담당자·기한처럼
    # AI가 이미 정한 부분은 살리고 못 정한 부분만 채우면 되게 한다. 없으면 빈 문자열.
    draft_hint = models.TextField(blank=True, default="")
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
        """진행자가 확인해야 하는 항목인가 — AI가 갈린다고 봤거나 규칙이 걸렸거나."""
        return bool(self.warnings or self.divergence_reason)

    @property
    def needs_host(self):
        """진행자가 손대야 하는 항목 — 확인이 필요하거나, 공개할 문장이 아직 없거나.

        화면의 묶음 분리와 배지가 이 판정을 쓴다. 문장 유무를 함께 보는 이유: 이번 재설계
        전에 만들어진 무경고 draft는 final_text가 비어 있어, '그대로 공개'로 분류하면 번호만
        있는 빈 줄이 공개된다.
        """
        return self.has_warning or not self.published_text

    @property
    def blocks_publish(self):
        """이 항목 때문에 공개를 막아야 하는가 — 공개할 문장이 없으면 막는다.

        조건은 이 하나다. '다음 회의로 넘김'을 고른 항목도 문장을 받으므로
        (decision_compiler.undecided_sentence) 여기서 갈라지지 않는다. 화면의 버튼 비활성
        판정과 서버의 거부가 이 프로퍼티 하나를 같이 쓴다.
        """
        return not self.published_text

    @property
    def published_text(self):
        """팀원에게 보여줄 확정 내용. 어떤 상태에서나 이 문장 하나다 — 폴백은 없다.

        명확한 결정은 AI가 쓴 문장, 확인이 필요했던 결정은 진행자가 고른 문장, '다음 회의로
        넘김'은 undecided_sentence()가 만든 문장이 여기 들어온다.
        """
        return self.final_text


class Interpretation(models.Model):
    """확인이 필요한 Decision 하나에 대해 AI가 만든, 그대로 공개될 확정 문장 후보.

    해석이 아니라 완성된 문장이다 — 진행자가 고르면 그 문장이 복사되어 final_text가 되고
    팀원에게 그대로 공개된다. 그래서 누가·무엇을·언제까지·완료 기준이 문장 안에 들어 있어야 한다.

    evidence는 녹취 원문에 그대로 등장하는 문장이어야 하며, 지어낸 근거가 붙은 후보는 저장
    단계에서 버린다(공백을 무시한 뒤 원문에 그 문장이 실제로 있는지 비교한다).
    정답은 없다 — recommended는 근거가 가장 강한 하나를 가리키는 배지일 뿐이고, 미리 선택해
    두지 않는다. 후보 목록 자체는 진행자에게만 보여주고 팀원에게는 보여주지 않는다.
    """

    decision = models.ForeignKey(Decision, on_delete=models.CASCADE, related_name="interpretations")
    order = models.PositiveSmallIntegerField(default=0)
    text = models.TextField()
    evidence = models.TextField(blank=True, default="")
    # 근거가 가장 강한 후보 하나에만 True. 화면의 'AI 추천' 배지로만 쓰고, 미리 선택해 두지는
    # 않는다 — 진행자가 읽고 직접 골라야 하기 때문이다.
    recommended = models.BooleanField(default=False)

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
    """공개된 결정에 대한 팀원별 확인 기록.

    kind=ack는 "확인했어요", kind=differ는 "제 이해와 달랐어요"다. comment는 differ에
    덧붙이는 한 줄 의견(선택)이다.

    (항목, 사용자) 쌍으로 유일하고 나중에 수정할 수 있다 — 진행자 집계는 항상
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
    """착수 체크포인트(Phase E). 공개된 결정에서 본인이 담당자인 항목에 "완료 모습 한 줄"을
    쓰면, AI가 확정된 결정과 대조해 '맞음 / 차이 있음(이유 한 줄)'을 돌려준다."""

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
