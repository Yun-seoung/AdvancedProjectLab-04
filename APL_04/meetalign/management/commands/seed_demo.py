from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from meetalign.models import Meeting, Team

TEAM_NAME = "4조"
DEMO_PASSWORD = "demo1234!"
MEETING_TITLE = "스터디앱 1차 배포 준비"

#: (아이디, 역할). 단일 팀을 가정하고 진행자 1명 + 참가자 2명을 만든다.
ACCOUNTS = [
    ("host", "진행자"),
    ("member1", "참가자"),
    ("member2", "참가자"),
]


class Command(BaseCommand):
    help = "시연용 데모 계정 3개(진행자 1, 참가자 2)와 단일 팀, 빈 회의 1개를 만든다."

    def handle(self, *args, **options):
        team, team_created = Team.objects.get_or_create(name=TEAM_NAME)
        self.stdout.write("팀: %s (%s)" % (team.name, "생성" if team_created else "기존"))

        users = []
        for username, role in ACCOUNTS:
            user, created = User.objects.get_or_create(username=username)
            user.set_password(DEMO_PASSWORD)
            user.save()
            team.members.add(user)
            users.append(user)
            self.stdout.write("  %s / %s  (%s, %s)"
                              % (username, DEMO_PASSWORD, role, "생성" if created else "비밀번호 갱신"))

        host = users[0]
        meeting, created = Meeting.objects.get_or_create(
            team=team, title=MEETING_TITLE, defaults={"host": host})
        if not created and meeting.host_id is None:
            meeting.host = host
            meeting.save()
        self.stdout.write("회의: %s (%s, 진행자 %s)"
                          % (meeting.title, "생성" if created else "기존", meeting.host.username))
        self.stdout.write(self.style.SUCCESS(
            "완료. host 로 로그인해 회의 상세에서 녹음 또는 전사 텍스트를 제출하세요."))
