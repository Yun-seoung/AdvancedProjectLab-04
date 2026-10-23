"""확인 퀴즈(이해도 검증 문항)를 아직 다 풀지 않은 참가자를 집계한다.

참가자를 따로 저장하는 모델은 없으므로, 회의가 속한 팀의 멤버(Team.members)를 참가자로 보고
진행자(Meeting.host)는 독려를 받을 대상이 아니라서 집계에서 뺀다. 진행자가 없는(기능 도입 전에
만든) 회의는 빼는 사람 없이 팀 멤버 전원을 대상으로 한다.

"푼 사람"은 '지금 공개된' 문항 전부에 응답한 사람이다. 녹음을 추가로 올리면 문항이 batch 단위로
더 공개될 수 있는데(PR #31), 그때는 분모인 공개 문항 수가 늘어나므로 새 문항에 답하지 않은
사람은 다시 미완료로 집계된다.

이름이 담긴 pending_participants()는 진행자 전용이다. 팀 홈 배지는 숫자만 쓰는
pending_count_for_meetings()를 쓴다.
"""

from django.db.models import Count

from .models import Answer, CheckQuestion, Team

# 배지에 그대로 쓰는 최대 숫자. 이보다 많으면 "99+"로 줄인다.
BADGE_MAX = 99


def badge_text(count):
    """배지에 표시할 문자열. 99를 넘으면 "99+"."""
    return "%d+" % BADGE_MAX if count > BADGE_MAX else str(count)


def _published_counts(meeting_ids):
    """{meeting_id: 공개된 문항 수} — 회의 개수와 무관하게 쿼리 1번."""
    rows = (
        CheckQuestion.objects
        .filter(meeting_id__in=meeting_ids, status=CheckQuestion.STATUS_PUBLISHED)
        .values("meeting_id")
        .annotate(n=Count("id"))
    )
    return {row["meeting_id"]: row["n"] for row in rows}


def _answered_counts(meeting_ids):
    """{(meeting_id, user_id): 공개 문항에 응답한 개수} — 쿼리 1번.

    공개되지 않은(초안) 문항에 달린 응답은 분자/분모 양쪽에서 모두 빠져야 하므로
    여기서도 published만 센다.
    """
    rows = (
        Answer.objects
        .filter(
            question__meeting_id__in=meeting_ids,
            question__status=CheckQuestion.STATUS_PUBLISHED,
        )
        .values("question__meeting_id", "user_id")
        .annotate(n=Count("id"))
    )
    return {(row["question__meeting_id"], row["user_id"]): row["n"] for row in rows}


def _member_ids_by_team(team_ids):
    """{team_id: [user_id, ...]} — 팀이 몇 개든 쿼리 1번."""
    rows = Team.members.through.objects.filter(team_id__in=team_ids).values_list("team_id", "user_id")
    grouped = {}
    for team_id, user_id in rows:
        grouped.setdefault(team_id, []).append(user_id)
    return grouped


def _target_ids(meeting, member_ids_by_team):
    """집계 대상(참가자 중 진행자 제외) user_id 목록."""
    members = member_ids_by_team.get(meeting.team_id, ())
    return [user_id for user_id in members if user_id != meeting.host_id]


def pending_count_for_meetings(meetings):
    """{meeting_id: 퀴즈를 아직 다 풀지 않은 참가자 수}. 공개된 퀴즈가 없으면 None.

    회의 목록 화면에서 쓰기 때문에 회의마다 쿼리를 날리지 않는다. 회의가 몇 개든
    멤버/공개 문항 수/응답 수 세 번의 집계 쿼리만 쓴다.
    """
    meetings = list(meetings)
    if not meetings:
        return {}
    meeting_ids = [meeting.id for meeting in meetings]
    totals = _published_counts(meeting_ids)
    answered = _answered_counts(meeting_ids)
    member_ids_by_team = _member_ids_by_team({meeting.team_id for meeting in meetings})

    counts = {}
    for meeting in meetings:
        total = totals.get(meeting.id, 0)
        if not total:
            # 처리 중이거나 진행자가 아직 공개하지 않은 회의는 아예 표시하지 않는다.
            counts[meeting.id] = None
            continue
        targets = _target_ids(meeting, member_ids_by_team)
        done = sum(1 for user_id in targets if answered.get((meeting.id, user_id), 0) >= total)
        counts[meeting.id] = len(targets) - done
    return counts


def participant_count(meeting):
    """집계 대상(진행자 제외 참가자) 인원."""
    members = meeting.team.members
    return members.exclude(pk=meeting.host_id).count() if meeting.host_id else members.count()


def pending_participants(meeting):
    """[(user, 응답한 문항 수, 전체 공개 문항 수), ...] — 아직 다 풀지 않은 사람만, 이름순.

    ※ 진행자 화면 전용. 이름이 들어 있으므로 호출 전에 반드시 진행자 권한을 확인해야 한다.
    공개된 퀴즈가 없으면 빈 목록을 돌려준다.
    """
    total = meeting.check_questions.filter(status=CheckQuestion.STATUS_PUBLISHED).count()
    if not total:
        return []
    answered = {
        row["user_id"]: row["n"]
        for row in (
            Answer.objects
            .filter(question__meeting=meeting, question__status=CheckQuestion.STATUS_PUBLISHED)
            .values("user_id")
            .annotate(n=Count("id"))
        )
    }
    members = meeting.team.members
    targets = members.exclude(pk=meeting.host_id) if meeting.host_id else members.all()
    pending = []
    for user in targets.order_by("username"):
        done = answered.get(user.id, 0)
        if done < total:
            pending.append((user, done, total))
    return pending
