from django.conf import settings
from django.db import models


class Team(models.Model):
    """목업에서 이어받은 팀. 프로토타입은 단일 팀만 쓰고 팀 기능을 더 늘리지 않는다."""
    name = models.CharField(max_length=100, unique=True)
    members = models.ManyToManyField(settings.AUTH_USER_MODEL, related_name="teams")

    def __str__(self):
        return self.name


class Meeting(models.Model):
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="meetings")
    title = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)
    ended = models.BooleanField(default=False)

    # 진행자: 회의를 만든 사람. 녹음 제출과 문제 검토/승인/집계는 이 사람만 할 수 있다.
    host = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="hosted_meetings",
    )
    transcript = models.TextField(blank=True, default="")   # 전사 텍스트 (STT 결과 또는 붙여넣기)
    minutes = models.TextField(blank=True, default="")      # 통합 회의록 (마크다운)
    quiz_published = models.BooleanField(default=False)  # 진행자가 참가자에게 공개했는지

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title

    @property
    def has_result(self):
        return bool(self.minutes)


class QuizItem(models.Model):
    """이해가 엇갈리기 쉬운 지점을 묻는 4지선다 확인 문제."""
    KINDS = [
        ("number", "수치"),
        ("deadline", "기한"),
        ("owner", "담당자"),
        ("term", "용어"),
        ("scope", "범위"),
        ("status", "결정됨 vs 논의 중"),
    ]

    meeting = models.ForeignKey(Meeting, on_delete=models.CASCADE, related_name="quiz_items")
    order = models.IntegerField(default=0)
    kind = models.CharField(max_length=20, choices=KINDS, default="status")
    text = models.TextField()
    choice1 = models.CharField(max_length=300)
    choice2 = models.CharField(max_length=300)
    choice3 = models.CharField(max_length=300)
    choice4 = models.CharField(max_length=300)
    answer_index = models.IntegerField(default=1)   # 1~4
    evidence = models.TextField(blank=True)         # 회의 내용 중 근거 문장

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        return "%s. %s" % (self.order, self.text[:30])

    @property
    def choices(self):
        """[(번호, 보기 텍스트), ...]"""
        return list(enumerate([self.choice1, self.choice2, self.choice3, self.choice4], start=1))

    @property
    def answer_text(self):
        return dict(self.choices).get(self.answer_index, "")


class Response(models.Model):
    """참가자의 답. 집계는 인원수만 쓰고 화면에 이름을 노출하지 않는다."""
    item = models.ForeignKey(QuizItem, on_delete=models.CASCADE, related_name="responses")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="responses")
    selected_index = models.IntegerField()
    is_correct = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("item", "user")]

    def __str__(self):
        return "item=%s selected=%s" % (self.item_id, self.selected_index)


class AnonQuestion(models.Model):
    """익명 질문. 작성자 필드를 두지 않는다 (로그에도 남기지 않는다)."""
    meeting = models.ForeignKey(Meeting, on_delete=models.CASCADE, related_name="questions")
    text = models.TextField()
    answer = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return self.text[:30]
