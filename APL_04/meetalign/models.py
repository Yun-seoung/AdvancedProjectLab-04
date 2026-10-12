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
    """회의별 대화방의 익명 질문. 작성자는 저장하지 않는다."""
    meeting = models.ForeignKey(Meeting, on_delete=models.CASCADE, related_name="questions")
    text = models.TextField()
    answer = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.text[:30]


class CheckQuestion(models.Model):
    """이해도 검증을 위해 (LLM이) 낸 질문. 회의당 여러 개이며 팀원 전원이 각자 답한다."""
    meeting = models.ForeignKey(Meeting, on_delete=models.CASCADE, related_name="check_questions")
    text = models.TextField()
    order = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["order", "id"]
        constraints = [models.UniqueConstraint(fields=["meeting", "order"], name="unique_check_question_order")]

    def __str__(self):
        return self.text[:30]


class Answer(models.Model):
    """CheckQuestion에 대한 팀원별 답변. 같은 질문이라도 사람마다 따로 저장되어야
    "누구와 누구가 다르게 이해했는지" 비교가 가능하다."""
    question = models.ForeignKey(CheckQuestion, on_delete=models.CASCADE, related_name="answers")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="check_answers")
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("question", "user")

    def __str__(self):
        return "%s - %s" % (self.user, self.question_id)
