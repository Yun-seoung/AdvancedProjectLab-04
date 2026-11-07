"""결정 확인 알림. 진행자가 확정 문장을 공개하거나 공개 뒤에 고치면, 진행자를 뺀 회의 참석자마다
Notification을 남긴다(한 사람에게 회의당 안 읽은 알림은 하나 — 여러 번 고쳐도 쌓이지 않는다). 팀원이 그 회의의 결정 확인 화면을 열면 그 회의 알림은 읽은 것으로 보고 바로 지운다 —
읽은 알림은 다시 볼 일이 없어 목록에 남겨 두면 새 알림을 가리기만 한다.

읽지 않은 알림은 세 곳에서 보인다 — 상단 메뉴의 알림 아이콘(context_processors.notifications),
회의 목록의 회의별 배지(unread_kinds_by_meeting), 회의 화면 위 안내(unread_for_meeting).
"""

from django.utils import timezone

from .models import Notification

MESSAGES = {
    Notification.KIND_PUBLISHED: "'%s' 회의의 결정이 공개됐어요",
    Notification.KIND_REVISED: "'%s' 회의의 결정이 수정됐어요",
}


def _recipients(meeting):
    """알림을 받을 사람 — 그 회의의 참석자 중 진행자를 뺀 사람(바꾼 사람은 받지 않는다).

    참석자(Meeting.participants)를 고르지 않은 회의(기능 도입 전에 만든 회의 포함)는 팀 멤버
    전원을 참석자로 본다. 참석자로 골랐어도 지금 팀을 떠난 사람은 뺀다 — 그 회의를 열 수 없다.
    """
    members = meeting.team.members.all()
    if meeting.participants.exists():
        members = members.filter(pk__in=meeting.participants.values("pk"))
    return members.exclude(pk=meeting.host_id) if meeting.host_id else members


def notify_decision_change(meeting, kind):
    """공개(KIND_PUBLISHED) 또는 수정(KIND_REVISED) 알림을 보낸다 — 한 사람에게 회의당 안 읽은 알림은 하나.

    아직 읽지 않은 알림이 있는 사람에게는 새 줄을 만들지 않고 그 줄을 최신으로 끌어올린다.
    종류는 이렇게 정한다:
    - 안 읽은 '공개' 알림 위에 수정이 오면 '공개'를 유지한다 — 그 사람은 아직 결정을 본 적이
      없으므로 처음 보는 결정일 뿐 '바뀐' 결정이 아니다.
    - 안 읽은 '수정' 알림 위에 수정이 또 오면 '수정' 한 줄로 합친다.
    - (공개 취소가 안 읽은 알림을 거두므로) 안 읽은 알림 위에 공개가 다시 오는 일은 드물지만,
      오면 기존 줄의 종류를 그대로 둔다.
    """
    now = timezone.now()
    recipients = list(_recipients(meeting))
    unread = {
        note.user_id: note
        for note in Notification.objects.filter(meeting=meeting, read_at__isnull=True, user__in=recipients)
    }
    fresh = []
    for user in recipients:
        note = unread.get(user.pk)
        if note is None:
            fresh.append(Notification(user=user, meeting=meeting, kind=kind, message=MESSAGES[kind] % meeting.title))
            continue
        # 회의 제목이 그사이 바뀌었을 수 있으니 문구도 다시 만든다. auto_now_add는 update()로만 바꿀 수 있다.
        Notification.objects.filter(pk=note.pk).update(created_at=now, message=MESSAGES[note.kind] % meeting.title)
    Notification.objects.bulk_create(fresh)


def withdraw_unread(meeting):
    """공개를 취소하면 아직 읽지 않은 알림을 거둔다 — 눌러도 볼 결정이 없기 때문이다."""
    Notification.objects.filter(meeting=meeting, read_at__isnull=True).delete()


def mark_read(user, meeting):
    """그 회의의 결정 확인 화면을 열었을 때 — 그 회의 알림을 읽은 것으로 보고 모두 지운다.

    예전에 read_at만 채워 남겨 둔 알림도 함께 지운다.
    """
    Notification.objects.filter(user=user, meeting=meeting).delete()


def clear_all(user):
    """알림 목록의 '모두 읽음' — 그 사람의 알림을 모두 지운다."""
    Notification.objects.filter(user=user).delete()


def unread_count(user):
    return Notification.objects.filter(user=user, read_at__isnull=True).count()


def unread_kinds_by_meeting(user, meetings):
    """{meeting_id: "revised" 또는 "published"} — 회의 목록 배지용, 회의 수와 무관하게 쿼리 1번.

    notify_decision_change가 회의당 안 읽은 알림을 하나로 유지하지만, 그 전에 쌓인 줄이 남아
    공개·수정 알림이 함께 있으면 '수정됨'을 보여준다 — 이미 본 결정이 바뀌었다는 쪽이 더 놓치면
    안 되는 소식이기 때문이다.
    """
    kinds = {}
    rows = (
        Notification.objects
        .filter(user=user, read_at__isnull=True, meeting__in=list(meetings))
        .values_list("meeting_id", "kind")
    )
    for meeting_id, kind in rows:
        if kinds.get(meeting_id) != Notification.KIND_REVISED:
            kinds[meeting_id] = kind
    return kinds


def unread_for_meeting(user, meeting):
    """회의 화면 위 안내용 — 그 회의에서 읽지 않은 알림의 종류. 없으면 None."""
    return unread_kinds_by_meeting(user, [meeting]).get(meeting.id)
