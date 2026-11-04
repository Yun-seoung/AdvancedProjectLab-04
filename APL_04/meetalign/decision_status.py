"""공개된 확정 명세를 아직 확인하지 않은 팀원을 집계한다.

참가자를 따로 저장하는 모델은 없으므로, 회의가 속한 팀의 멤버(Team.members)를 참가자로 보고
진행자(Meeting.host)는 독려를 받을 대상이 아니라서 집계에서 뺀다. 진행자가 없는(기능 도입 전에
만든) 회의는 빼는 사람 없이 팀 멤버 전원을 대상으로 한다.

"확인한 사람"은 '지금 공개된(published)' 항목 전부에 DecisionAgreement를 남긴 사람이다.
kind는 보지 않는다 — '확인했어요'와 '제 이해와 달랐어요'는 둘 다 확인을 마친 것이고, 어느
쪽을 골랐는지는 이 모듈의 결과에 담기지 않는다. '아직 미정'으로 넘어간(undecided) 항목은
읽기 전용이라 분모·분자 양쪽에서 빠진다.

진행자가 공개 뒤에 확정 문장을 고치면 그 항목의 확인 기록이 모두 지워지므로(decision_compiler.
revise_final_text), 이미 확인했던 팀원도 다시 미확인으로 집계된다.

이름이 담긴 pending_participants()는 진행자 전용이다. 팀 홈 배지는 숫자만 쓰는
pending_count_for_meetings()를 쓴다.
"""

from django.db.models import Count

from .models import Decision, DecisionAgreement, Team

# 배지에 그대로 쓰는 최대 숫자. 이보다 많으면 "99+"로 줄인다.
BADGE_MAX = 99


def badge_text(count):
    """배지에 표시할 문자열. 99를 넘으면 "99+"."""
    return "%d+" % BADGE_MAX if count > BADGE_MAX else str(count)


def _published_counts(meeting_ids):
    """{meeting_id: 공개된 항목 수} — 회의 개수와 무관하게 쿼리 1번."""
    rows = (
        Decision.objects
        .filter(meeting_id__in=meeting_ids, status=Decision.STATUS_PUBLISHED)
        .values("meeting_id")
        .annotate(n=Count("id"))
    )
    return {row["meeting_id"]: row["n"] for row in rows}


def _acked_counts(meeting_ids):
    """{(meeting_id, user_id): 확인한 공개 항목 수} — 쿼리 1번.

    미결정(undecided)이나 아직 공개하지 않은(draft) 항목에 달린 확인 기록은 분모에서 빠지므로
    여기서도 published만 센다. kind는 조회하지 않는다.
    """
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
    """{meeting_id: 아직 결정을 확인하지 않은 팀원 수}. 공개된 항목이 없으면 None.

    회의 목록 화면에서 쓰기 때문에 회의마다 쿼리를 날리지 않는다. 회의가 몇 개든
    멤버/공개 항목 수/확인 수 세 번의 집계 쿼리만 쓴다.
    """
    meetings = list(meetings)
    if not meetings:
        return {}
    meeting_ids = [meeting.id for meeting in meetings]
    totals = _published_counts(meeting_ids)
    acked = _acked_counts(meeting_ids)
    member_ids_by_team = _member_ids_by_team({meeting.team_id for meeting in meetings})

    counts = {}
    for meeting in meetings:
        total = totals.get(meeting.id, 0)
        if not total:
            # 아직 공개하지 않았거나 전부 미결정으로 넘어간 회의는 아예 표시하지 않는다.
            counts[meeting.id] = None
            continue
        targets = _target_ids(meeting, member_ids_by_team)
        done = sum(1 for user_id in targets if acked.get((meeting.id, user_id), 0) >= total)
        counts[meeting.id] = len(targets) - done
    return counts


def participant_count(meeting):
    """집계 대상(진행자 제외 참가자) 인원."""
    members = meeting.team.members
    return members.exclude(pk=meeting.host_id).count() if meeting.host_id else members.count()


def pending_participants(meeting):
    """[(user, 확인한 항목 수, 전체 공개 항목 수), ...] — 아직 다 확인하지 않은 사람만, 이름순.

    ※ 진행자 화면 전용. 이름이 들어 있으므로 호출 전에 반드시 진행자 권한을 확인해야 한다.
    공개된 항목이 없으면 빈 목록을 돌려준다.
    """
    total = meeting.decisions.filter(status=Decision.STATUS_PUBLISHED).count()
    if not total:
        return []
    acked = {
        row["user_id"]: row["n"]
        for row in (
            DecisionAgreement.objects
            .filter(decision__meeting=meeting, decision__status=Decision.STATUS_PUBLISHED)
            .values("user_id")
            .annotate(n=Count("id"))
        )
    }
    members = meeting.team.members
    targets = members.exclude(pk=meeting.host_id) if meeting.host_id else members.all()
    pending = []
    for user in targets.order_by("username"):
        done = acked.get(user.id, 0)
        if done < total:
            pending.append((user, done, total))
    return pending
