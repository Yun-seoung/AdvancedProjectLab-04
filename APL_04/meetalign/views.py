import os

from django.contrib.auth import login, logout
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import FileResponse, Http404, HttpResponse
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.http import url_has_allowed_host_and_scheme

from . import decision_compiler, notifications, services, tasks
from .models import (
    ChatMessage, Decision, Interpretation, Meeting, Notification,
    Question, Recording, Team,
)

ALLOWED_RECORDING_EXTENSIONS = {
    ".mp3", ".m4a", ".wav", ".ogg", ".webm", ".mp4", ".flac",
    ".aac", ".wma", ".opus", ".amr", ".caf", ".avi", ".mov", ".mkv", ".3gp",
}
MAX_RECORDING_BYTES = 500 * 1024 * 1024


def index(request):
    teams = request.user.teams.all() if request.user.is_authenticated else []
    return render(request, "meetalign/index.html", {"teams": teams})


def signup(request):
    if request.method == "POST":
        form = UserCreationForm(request.POST)
        if form.is_valid():
            login(request, form.save())
            return redirect("index")
    else:
        form = UserCreationForm()
    return render(request, "meetalign/signup.html", {"form": form})


def login_view(request):
    if request.method == "POST":
        form = AuthenticationForm(request, data=request.POST)
        if form.is_valid():
            login(request, form.get_user())
            next_url = request.GET.get("next", "")
            if url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
                return redirect(next_url)
            return redirect("index")
    else:
        form = AuthenticationForm(request)
    return render(request, "meetalign/login.html", {"form": form})


def logout_view(request):
    logout(request)
    return redirect("index")


@login_required
def team_join(request):
    error = ""
    if request.method == "POST":
        if request.POST.get("action") == "create":
            name = request.POST.get("name", "").strip()
            if not name or Team.objects.filter(name=name).exists():
                error = "팀 이름이 비었거나 이미 존재합니다."
            else:
                team = Team.objects.create(name=name, created_by=request.user)
                team.members.add(request.user)
                return redirect("meeting_list", team_id=team.id)
        else:
            code = request.POST.get("code", "").strip().upper()
            team = Team.objects.filter(invite_code=code).first()
            if team is None:
                error = "해당 초대 코드의 팀이 없습니다."
            else:
                team.members.add(request.user)
                return redirect("meeting_list", team_id=team.id)
    return render(request, "meetalign/team_join.html", {"error": error})


def _team(request, team_id):
    return get_object_or_404(Team, pk=team_id, members=request.user)


def _meeting(request, meeting_id):
    return get_object_or_404(Meeting, pk=meeting_id, team__members=request.user)


def _meeting_rows(meetings, user):
    """회의 목록에 뿌릴 (회의 + 결정 알림 배지) 묶음.

    배지는 '이 사람이 아직 읽지 않은 결정 알림'이 있는 회의에만 붙는다(공개 → "새 결정",
    공개 뒤 수정 → "수정됨"). 회의 개수와 무관하게 쿼리 1번으로 모은다(N+1 금지).
    """
    kinds = notifications.unread_kinds_by_meeting(user, meetings)
    rows = []
    for meeting in meetings:
        kind = kinds.get(meeting.id)
        rows.append({
            "meeting": meeting,
            "notice": kind,
            "notice_label": "수정됨" if kind == Notification.KIND_REVISED else "새 결정" if kind else "",
        })
    return rows


@login_required
def meeting_list(request, team_id):
    team = _team(request, team_id)
    if request.method == "POST" and request.POST.get("action") == "delete_team":
        if team.created_by_id != request.user.id:
            raise PermissionDenied
        team.delete()
        return redirect("index")
    meetings = list(team.meetings.all())
    return render(request, "meetalign/meeting_list.html", {
        "team": team, "meetings": meetings, "rows": _meeting_rows(meetings, request.user),
    })


def _parse_deadline(raw):
    if not raw:
        return None, None
    try:
        parsed = parse_date(raw)
    except ValueError:
        parsed = None
    if parsed is None:
        return None, "마감일 형식이 올바르지 않습니다."
    if parsed < timezone.localdate():
        return None, "마감일은 오늘 이후 날짜로 설정해주세요."
    return parsed, None


def _recording_error(recording):
    if os.path.splitext(recording.name)[1].lower() not in ALLOWED_RECORDING_EXTENSIONS:
        return "지원하지 않는 녹음 형식입니다 (" + ", ".join(sorted(ext.lstrip(".") for ext in ALLOWED_RECORDING_EXTENSIONS)) + ")."
    if recording.size > MAX_RECORDING_BYTES:
        return "녹음 파일이 너무 큽니다 (500MB 이하만 가능)."
    return None


def _team_members(team):
    return list(team.members.order_by("username"))


def _chosen_participants(request, team, host):
    """폼에서 고른 참석자. 팀 멤버가 아닌 id는 버리고, 진행자는 언제나 넣는다."""
    ids = {raw for raw in request.POST.getlist("participants") if raw.isdigit()}
    chosen = [member for member in team.members.all() if str(member.pk) in ids]
    if host is not None and all(member.pk != host.pk for member in chosen):
        chosen.append(host)
    return chosen


def _participant_ids(meeting):
    """참석자 칸에 체크해 둘 id. 참석자를 고르지 않은 회의는 팀 멤버 전원(알림 범위와 같은 기준)."""
    ids = set(meeting.participants.values_list("pk", flat=True))
    return ids or set(meeting.team.members.values_list("pk", flat=True))


def _participant_context(meeting, user):
    """회의 화면의 참석자 칸(진행자에게만 보인다) — 결정 알림을 받을 범위."""
    if meeting.host_id != user.id:
        return {"team_members": [], "participant_ids": set(), "host_id": meeting.host_id}
    return {
        "team_members": _team_members(meeting.team),
        "participant_ids": _participant_ids(meeting),
        "host_id": meeting.host_id,
    }


@login_required
def meeting_create(request, team_id):
    team = _team(request, team_id)
    error = None
    if request.method == "POST" and request.POST.get("title", "").strip():
        deadline, error = _parse_deadline(request.POST.get("deadline", "").strip())
        if error is None:
            meeting = Meeting.objects.create(team=team, title=request.POST["title"].strip(), deadline=deadline, host=request.user)
            meeting.participants.set(_chosen_participants(request, team, request.user))
            return redirect("meeting_detail", meeting_id=meeting.id)
    return render(request, "meetalign/meeting_create.html", {
        "team": team, "error": error, "team_members": _team_members(team),
        # 처음에는 팀 전원을 체크해 둔다 — 빼는 쪽이 고르는 쪽보다 손이 덜 간다.
        "participant_ids": {member.pk for member in team.members.all()}, "host_id": request.user.id,
    })


@login_required
def meeting_detail(request, meeting_id):
    meeting = _meeting(request, meeting_id)
    error = None
    if request.method == "POST":
        recording = request.FILES.get("recording")
        if request.POST.get("action") == "delete_meeting":
            if meeting.host_id != request.user.id:
                raise PermissionDenied
            team_id = meeting.team_id
            meeting.delete()
            return redirect("meeting_list", team_id=team_id)
        if request.POST.get("action") == "set_participants":
            if meeting.host_id != request.user.id:
                raise PermissionDenied
            meeting.participants.set(_chosen_participants(request, meeting.team, meeting.host))
            return redirect("meeting_detail", meeting_id=meeting.id)
        if recording and meeting.ended:
            error = "종료된 회의에는 녹음을 올릴 수 없습니다."
        elif recording:
            error = _recording_error(recording)
        if error:
            return render(request, "meetalign/meeting_detail.html", {
                "meeting": meeting, "error": error, "stt_status": services.stt_status_text(),
                **_participant_context(meeting, request.user),
            })
        if recording:
            added = Recording.objects.create(meeting=meeting, file=recording, status=Recording.STATUS_PROCESSING)
            tasks.start_processing(added.id)
        if request.POST.get("action") == "end":
            Meeting.objects.filter(pk=meeting.pk).update(ended=True)
            return redirect("meeting_detail", meeting_id=meeting.id)
        return redirect("meeting_result", meeting_id=meeting.id)
    return render(request, "meetalign/meeting_detail.html", {
        "meeting": meeting, "stt_status": services.stt_status_text(),
        # 결정 사항 추출 기능(실험): 지난 회의의 미결정 안건. 읽기만 하므로 위 POST 흐름은 건드리지 않는다.
        # '처리됨' 버튼은 별도 URL(decision_carryover_resolve)로 보낸다.
        "carryover_decisions": decision_compiler.carryover_decisions(meeting),
        # 이 회의에서 읽지 않은 결정 알림(공개·수정) — 화면 위 안내로 보여준다.
        "decision_notice": notifications.unread_for_meeting(request.user, meeting),
        "notice_revised": Notification.KIND_REVISED,
        **_participant_context(meeting, request.user),
    })


@login_required
def meeting_result(request, meeting_id):
    meeting = _meeting(request, meeting_id)
    tasks.recover_stuck(meeting)
    return render(request, "meetalign/meeting_result.html", {"meeting": meeting})


@login_required
def recording_file(request, meeting_id, recording_id):
    meeting = _meeting(request, meeting_id)
    recording = get_object_or_404(Recording, pk=recording_id, meeting=meeting)
    return FileResponse(recording.file.open("rb"), filename=recording.filename)


@login_required
def recording_retry(request, meeting_id, recording_id):
    meeting = _meeting(request, meeting_id)
    recording = get_object_or_404(Recording, pk=recording_id, meeting=meeting, status=Recording.STATUS_FAILED)
    if request.method == "POST":
        Recording.objects.filter(pk=recording.pk).update(status=Recording.STATUS_PROCESSING, error_message="")
        tasks.start_processing(recording.id)
    return redirect("meeting_result", meeting_id=meeting.id)


CHAT_HISTORY_TURNS = 3


@login_required
def chat(request, meeting_id):
    meeting = _meeting(request, meeting_id)
    mode = request.POST.get("mode") or request.GET.get("mode", "llm")
    error = None
    if request.method == "POST" and request.POST.get("text", "").strip():
        text = request.POST["text"].strip()
        if mode == "anon":
            if meeting.ended:
                error = "종료된 회의에는 익명 질문을 보낼 수 없습니다."
            else:
                Question.objects.create(meeting=meeting, text=text, user=request.user)
        else:
            history = list(
                ChatMessage.objects.filter(meeting=meeting, user=request.user)
                .order_by("-created_at")[: CHAT_HISTORY_TURNS * 2]
                .values_list("role", "text")
            )[::-1]
            ChatMessage.objects.create(meeting=meeting, user=request.user, role=ChatMessage.ROLE_USER, text=text)
            reply = services.chat_reply(meeting, text, history=history)
            ChatMessage.objects.create(meeting=meeting, user=request.user, role=ChatMessage.ROLE_ASSISTANT, text=reply)
            return redirect("%s?mode=llm" % reverse("chat", args=[meeting.id]))
    messages = []
    if mode != "anon":
        messages = ChatMessage.objects.filter(meeting=meeting, user=request.user)
    return render(request, "meetalign/chat.html", {"meeting": meeting, "mode": mode, "messages": messages, "error": error})


def _require_host(meeting, user):
    # 주최자가 없는(이 기능 도입 전에 만든) 회의는 팀원 누구나 허용한다.
    if meeting.host_id is not None and meeting.host_id != user.id:
        raise PermissionDenied


@login_required
def inbox(request, meeting_id):
    # 주최자는 받은 질문 전체(작성자 비공개)를 보고 답한다. 팀원은 본인이 보낸 질문과
    # 그 답변만 볼 수 있다 — 익명이라는 취지상 다른 사람이 받은 답은 보이면 안 된다.
    meeting = _meeting(request, meeting_id)
    is_host = meeting.host_id == request.user.id
    questions = meeting.questions.all() if is_host else meeting.questions.filter(user=request.user)
    return render(request, "meetalign/inbox.html", {"meeting": meeting, "questions": questions, "is_host": is_host})


@login_required
def summary(request, meeting_id):
    return render(request, "meetalign/summary.html", {"meeting": _meeting(request, meeting_id)})


@login_required
def answers(request, meeting_id):
    meeting = _meeting(request, meeting_id)
    _require_host(meeting, request.user)
    if request.method == "POST":
        question = get_object_or_404(Question, pk=request.POST.get("question_id"), meeting=meeting)
        question.answer = request.POST.get("answer", "").strip()
        question.save()
        return redirect("answers", meeting_id=meeting.id)
    return render(request, "meetalign/answers.html", {"meeting": meeting, "questions": meeting.questions.all()})


@login_required
def record_file(request, meeting_id):
    meeting = _meeting(request, meeting_id)
    if not meeting.record:
        raise Http404
    response = HttpResponse(meeting.record, content_type="text/plain; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="meeting_record.txt"'
    return response


# ---------------------------------------------------------------------------
# 결정 사항 추출 기능. 회의 원문에서 뽑은 결정 사항을 진행자가 확정해 공개하고, 팀원이
# 항목마다 확인 여부를 남기는 흐름이다.
# ---------------------------------------------------------------------------


def _decision_meeting(request, meeting_id):
    """결정 사항 추출 기능을 쓸 수 있는 회의만 통과시킨다.

    팀 밖 사용자는 _meeting()이 404로 막고, 회의록이 아직 완성되지 않은 회의는 여기서 404다
    — 추출할 녹취 원문이 없는 회의에 링크를 노출하지 않기 위함(템플릿 쪽도 같은 조건).
    """
    meeting = _meeting(request, meeting_id)
    if meeting.processing_status != Meeting.STATUS_DONE:
        raise Http404
    return meeting


def _reset_decisions(meeting):
    """추출 결과를 전부 지운다. 공개된 뒤에는 거부한다.

    팀원이 이미 본 내용이 말없이 사라지면 안 되기 때문이다. 다시 하려면 먼저 '공개 취소'로
    draft로 내려야 한다.
    """
    if meeting.decisions.exclude(status=Decision.STATUS_DRAFT).exists():
        return "이미 공개된 항목이 있어 결정 사항을 지울 수 없습니다. '공개 취소'를 먼저 눌러 주세요."
    meeting.decisions.all().delete()
    return None


def _unpublish_decisions(meeting):
    """공개를 되돌려 draft로 내린다 — 공개 직후 "잘못 눌렀다"를 수습하는 통로.

    팀원 확인을 받지 않으므로 언제든 되돌릴 수 있다. 아직 읽지 않은 알림은 거둔다 — 눌러도 볼
    결정이 없기 때문이다.
    """
    published = meeting.decisions.exclude(status=Decision.STATUS_DRAFT)
    if not published.exists():
        return "공개된 항목이 없습니다."
    published.update(status=Decision.STATUS_DRAFT, revised=False)
    notifications.withdraw_unread(meeting)
    return None


def _planned_choice(request, decision):
    """POST에서 이 항목의 선택을 읽어 ((종류, 값) 또는 None, 실패 사유 또는 None)을 돌려준다.

    읽기만 하고 쓰지 않는다 — 한 제출 안의 모든 선택을 먼저 검증한 뒤 한꺼번에 적용하기
    위함이다. 그러지 않으면 뒤쪽 항목이 거부당했는데 앞쪽의 '이 결정 빼기'는 이미 커밋되어,
    진행자가 되돌릴 방법이 없다(다시 추출하면 다른 선택도 전부 사라진다).
    """
    raw = request.POST.get("choice_%d" % decision.id, "")
    if not raw:
        return None, None

    if raw in (decision_compiler.CHOICE_DROP, decision_compiler.CHOICE_UNDECIDED):
        return (raw, None), None

    if raw == decision_compiler.CHOICE_CUSTOM:
        text = request.POST.get("custom_text_%d" % decision.id, "").strip()
        if not text:
            return None, "'직접 쓰기'를 골랐으면 확정 문장을 한 줄 적어 주세요."
        return (decision_compiler.CHOICE_CUSTOM, text), None

    if raw.startswith(decision_compiler.CHOICE_CANDIDATE_PREFIX):
        candidate_id = raw[len(decision_compiler.CHOICE_CANDIDATE_PREFIX):]
        if not candidate_id.isdigit():
            return None, "확정 방법을 하나 골라 주세요."
        interpretation = get_object_or_404(Interpretation, pk=candidate_id, decision=decision)
        # 고른 시점의 문장을 복사해 둔다. 뒤에 후보 문장이 바뀌어도 확정 내용은 따라 바뀌지 않는다.
        return (decision_compiler.CHOICE_CANDIDATE_PREFIX, interpretation.text), None

    return None, "확정 방법을 하나 골라 주세요."


def _apply_planned_choice(decision, kind, value):
    """검증을 통과한 선택 하나를 적용한다. draft인 동안에만 쓴다(동시 요청 대비 조건부 쓰기)."""
    draft_only = Decision.objects.filter(pk=decision.pk, status=Decision.STATUS_DRAFT)
    if kind == decision_compiler.CHOICE_DROP:
        # 회의에 없던 결정을 지운다. Interpretation은 cascade로 함께 사라진다.
        draft_only.delete()
        return
    if kind == decision_compiler.CHOICE_UNDECIDED:
        # 미정 항목도 문장을 갖는다 — 팀원 화면과 이월 목록이 final_text 하나만 보기 때문이다.
        draft_only.update(
            final_text=decision_compiler.undecided_sentence(decision.what), marked_undecided=True)
        return
    draft_only.update(final_text=value, marked_undecided=False)


def _save_decision_edits(request, meeting, publishing=False):
    """진행자의 확정 선택을 저장한다. draft 항목만 대상이다.

    고를 수 있는 것은 [AI 추천 문장 | 직접 쓰기 | 다음 회의로 넘김 | 이 결정 빼기]뿐이다.
    라디오를 고르지 않은 항목은 이렇게 다룬다.
    - 그대로 공개 항목(경고 없음): '직접 쓰기' 칸에 적힌 문장을 확정 문장으로 쓴다. 칸에는 AI가 쓴
      문장이 미리 들어 있으므로, 진행자가 칸만 고치면 라디오를 누르지 않아도 고친 문장이 공개된다.
      칸을 비웠으면 원래 문장을 그대로 둔다.
    - 확인 필요 항목: 저장할 때는 그대로 두고, 공개할 때(publishing=True)는 '이 결정 빼기'로
      처리한다 — 모든 항목을 고르지 않아도 공개할 수 있게 하되, 진행자가 확정하지 않은 모호한
      결정이 팀원에게 나가지 않게 한다. 다만 이렇게 빼면 공개할 항목이 하나도 남지 않는 경우에는
      빼지 않고 사유를 돌려준다(되돌릴 수 없는 삭제로 회의 결정이 통째로 사라지지 않게).

    모든 선택을 먼저 검증한다. 하나라도 잘못되면 **되돌릴 수 없는 '이 결정 빼기'만 보류**하고
    나머지는 적용한다 — 진행자가 고른 것이 사라지지 않아야 하고(화면이 DB를 다시 읽어 그리므로
    적용하지 않으면 그 선택은 화면에서도 사라진다), 동시에 실패한 제출이 삭제를 커밋해 복구
    불가능한 상태를 만들어서도 안 된다. 실패 사유 문자열 또는 None을 돌려준다.
    """
    planned = []
    implicit_drops = []
    error = None
    drafts = []
    for decision in meeting.decisions.all():
        if decision.status != Decision.STATUS_DRAFT:
            # 공개된 항목은 선택을 받지 않는다. 다만 '이 결정 빼기'는 조용히 무시하지 않고
            # 사유를 돌려준다 — 진행자가 왜 안 됐는지 알아야 '확정 문장 수정'으로 갈 수 있다.
            if request.POST.get("choice_%d" % decision.id, "") == decision_compiler.CHOICE_DROP and error is None:
                error = "공개된 항목은 뺄 수 없습니다. 내용을 고치려면 '확정 문장 수정'을 쓰세요."
            continue
        drafts.append(decision)
        plan, failed = _planned_choice(request, decision)
        if failed and error is None:
            # 어느 항목인지 함께 알려 준다 — 항목이 많으면 사유만으로는 찾을 수 없다.
            error = "'%s': %s" % (decision.what, failed)
        elif plan:
            planned.append((decision, plan))
        elif not failed and not decision.marked_undecided:
            if not decision.needs_host:
                text = request.POST.get("custom_text_%d" % decision.id, "").strip()
                if text and text != decision.final_text:
                    planned.append((decision, (decision_compiler.CHOICE_CUSTOM, text)))
            elif publishing and decision.blocks_publish:
                implicit_drops.append((decision, (decision_compiler.CHOICE_DROP, None)))
    if implicit_drops and error is None:
        dropped = {decision.pk for decision, _ in implicit_drops} | {
            decision.pk for decision, plan in planned if plan[0] == decision_compiler.CHOICE_DROP
        }
        if all(decision.pk in dropped for decision in drafts):
            error = "고른 결정이 하나도 없어 공개하면 모든 결정이 빠집니다. 하나 이상 골라 주세요."
        else:
            planned.extend(implicit_drops)
    if error:
        # 삭제는 되돌릴 수 없으므로 제출이 깨끗할 때만 한다. 나머지 선택은 그대로 저장한다.
        planned = [
            (decision, plan) for decision, plan in planned
            if plan[0] != decision_compiler.CHOICE_DROP
        ]
    with transaction.atomic():
        for decision, (kind, value) in planned:
            _apply_planned_choice(decision, kind, value)
    return error


def _publish_decisions(meeting):
    """"확정 문장 공개" — draft 항목을 팀원에게 공개한다.

    고르지 않은 확인 필요 항목은 바로 앞의 _save_decision_edits(publishing=True)가 이미 뺐다.
    그래도 공개할 문장이 없는 항목이 남아 있으면 거부한다(마지막 그물).
    """
    drafts = list(meeting.decisions.filter(status=Decision.STATUS_DRAFT))
    if not drafts:
        if not meeting.decisions.exists():
            return "모든 결정을 뺐습니다. 다시 추출해 주세요."
        return "공개할 항목이 없습니다."
    blockers = decision_compiler.publish_blockers(drafts)
    if blockers:
        return "공개할 문장이 없는 항목이 %d개 있습니다. 추천 문장을 고르거나 직접 쓰거나 '다음 회의로 넘김'을 골라 주세요." % len(blockers)
    decision_compiler.publish_decisions(meeting)
    notifications.notify_decision_change(meeting, Notification.KIND_PUBLISHED)
    return None


def _revise_final_text(request, meeting):
    """공개된 항목의 확정 문장을 고친다. 팀원에게 수정 알림이 간다."""
    decision = get_object_or_404(
        Decision, pk=request.POST.get("decision_id"), meeting=meeting,
        status=Decision.STATUS_PUBLISHED,
    )
    text = request.POST.get("final_text", "").strip()
    if not text:
        return "확정 문장을 한 줄 적어 주세요."
    if text == decision.final_text:
        # 같은 문장이면 바뀐 것이 없으므로 알림도 보내지 않는다.
        return None
    decision_compiler.revise_final_text(decision, text)
    notifications.notify_decision_change(meeting, Notification.KIND_REVISED)
    return None


def _resolve_carryover(meeting, decision_id):
    """이월된 미결정 안건을 '처리됨'으로 닫는다. 이후 회의 화면에서 사라진다."""
    decision = get_object_or_404(
        Decision, pk=decision_id, meeting__team_id=meeting.team_id,
        status=Decision.STATUS_UNDECIDED, carryover_resolved_at__isnull=True,
    )
    Decision.objects.filter(pk=decision.pk).update(carryover_resolved_at=timezone.now())


@login_required
def decision_carryover_resolve(request, meeting_id, decision_id):
    """이월 안건 '처리됨' 전용 엔드포인트.

    회의 상세 화면(meeting_detail)의 기존 POST 흐름(녹음 업로드/종료/삭제)을 건드리지 않기
    위해 별도 URL로 뺐다. 지금 보고 있는 회의의 진행자만 누를 수 있다.
    """
    meeting = _meeting(request, meeting_id)
    _require_host(meeting, request.user)
    if request.method == "POST":
        _resolve_carryover(meeting, decision_id)
    return redirect("meeting_detail", meeting_id=meeting.id)



def _current_choice(decision, interpretations):
    """진행자 확정 라디오에서 지금 선택돼 있어야 하는 값과, '직접 작성' 입력칸에 넣을 문장.

    고른 시점에 후보 문장을 final_text로 복사해 두므로, 같은 문장을 가진 후보가 있으면 그
    후보가 선택된 것으로 본다. 어느 후보와도 다르면 직접 작성한 것이다.
    """
    if decision.marked_undecided:
        return decision_compiler.CHOICE_UNDECIDED, ""
    if not decision.has_warning:
        # 확인이 필요 없는 항목은 아무것도 선택돼 있지 않아야 한다('고치기'는 접혀 있다).
        # '직접 쓰기' 칸에는 AI가 쓴 문장을 미리 넣어 고치기 쉽게 한다.
        return "", decision.final_text
    if not decision.final_text:
        return "", ""
    for interpretation in interpretations:
        if interpretation.text == decision.final_text:
            return "%s%d" % (decision_compiler.CHOICE_CANDIDATE_PREFIX, interpretation.id), ""
    return decision_compiler.CHOICE_CUSTOM, decision.final_text


@login_required
def decision_review(request, meeting_id):
    """진행자 전용. 추출 → 결정 사항·경고 확인과 수정 → 항목별 확정 → "확정된 결정 공개"."""
    meeting = _decision_meeting(request, meeting_id)
    _require_host(meeting, request.user)
    error = None
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "compile":
            try:
                decision_compiler.compile_meeting(meeting)
            except decision_compiler.DecisionCompileError as exc:
                error = str(exc)
        elif action == "reset":
            error = _reset_decisions(meeting)
        elif action == "save":
            error = _save_decision_edits(request, meeting)
        elif action == "publish":
            # 공개 버튼이 화면의 선택을 함께 저장한 뒤 공개한다. 저장과 공개가 나뉘어 있어서
            # 라디오를 다 고르고 공개를 눌렀는데 "처리하지 않은 항목이 있습니다"가 나오는 것이
            # 진행자가 가장 자주 걸리던 지점이었다. 저장이 실패하면 저장된 만큼은 남기고
            # 사유를 보여준다('저장' 버튼은 중간 저장용으로 그대로 둔다).
            error = _save_decision_edits(request, meeting, publishing=True) or _publish_decisions(meeting)
        elif action == "unpublish":
            error = _unpublish_decisions(meeting)
        elif action == "revise":
            error = _revise_final_text(request, meeting)
        if error is None:
            return redirect("decision_review", meeting_id=meeting.id)

    decisions = list(meeting.decisions.prefetch_related("interpretations"))
    rows = []
    for decision in decisions:
        interpretations = list(decision.interpretations.all())
        choice, custom_text = _current_choice(decision, interpretations)
        for interpretation in interpretations:
            # 라디오 value와 선택 여부는 템플릿 필터로 만들기 까다로워서(접두사 + 정수 id)
            # 여기서 붙여 둔다. 저장되지 않는 임시 속성이다.
            interpretation.choice_value = "%s%d" % (
                decision_compiler.CHOICE_CANDIDATE_PREFIX, interpretation.id,
            )
            interpretation.chosen = choice == interpretation.choice_value
        rows.append({
            "decision": decision,
            "interpretations": interpretations,
            "choice": choice,
            "custom_text": custom_text,
            # 근거 한 문장만으론 판단하기 어려울 때 앞뒤 맥락을 더 보여준다.
            "evidence_context": decision_compiler.context_around(meeting.transcript, decision.evidence),
            # 경고에 찍힌 표현(예: '그쪽')이 원문 어디서 나왔는지. 근거 문장 위치와 다를 수
            # 있어 따로 찾는다 — warning_context 참고.
            "warning_rows": [
                {"text": warning, "context": decision_compiler.warning_context(meeting.transcript, warning)}
                for warning in decision.warnings
            ],
            # 확인이 필요한데 아직 고르지 않은 항목 — 이대로 공개하면 빠진다.
            "blocking": decision.blocks_publish,
        })
    # 확인이 필요한 항목과 그대로 공개될 항목을 따로 묶는다. AI 모호 판정은 warnings가 아니라
    # divergence_reason에 담기므로 기준은 has_warning이다.
    # '다음 회의로 넘김'을 고른 항목은 공개되지 않고 이월된다. 세 묶음으로 나누지 않으면
    # 화면은 "그대로 공개된다"고 말하고 서버는 "넘긴다"로 처리해 서로 다른 말을 한다.
    carried_rows = [row for row in rows if row["decision"].marked_undecided]
    ambiguous_rows = [
        row for row in rows
        if row["decision"].needs_host and not row["decision"].marked_undecided
    ]
    clear_rows = [
        row for row in rows
        if not row["decision"].needs_host and not row["decision"].marked_undecided
    ]
    for group, group_rows in (("ambiguous", ambiguous_rows), ("clear", clear_rows), ("carried", carried_rows)):
        for row in group_rows:
            row["group"] = group
    # 화면은 한 문항씩 넘겨 본다 — 확인이 필요한 것부터, 그다음 그대로 공개될 것, 넘길 것 순서.
    steps = ambiguous_rows + clear_rows + carried_rows
    drafts = [row for row in rows if row["decision"].status == Decision.STATUS_DRAFT]
    blockers = [row for row in drafts if row["blocking"]]
    return render(request, "meetalign/decision_review.html", {
        "meeting": meeting,
        "rows": rows,
        "steps": steps,
        "error": error,
        "compiled": bool(decisions),
        "ambiguous_rows": ambiguous_rows,
        "clear_rows": clear_rows,
        "carried_rows": carried_rows,
        "ambiguous_count": len(ambiguous_rows),
        # 확인창이 "공개 N개 · 이월 M개"를 정확히 말할 수 있게 draft만 나눠 센다.
        "publish_count": sum(
            1 for row in drafts if not row["decision"].marked_undecided),
        "carried_count": sum(
            1 for row in drafts if row["decision"].marked_undecided),
        "draft_count": len(drafts),
        "blocker_count": len(blockers),
        # 고르지 않은 항목이 있어도 공개할 수 있다 — 그 항목은 공개할 때 빠진다.
        "can_publish": bool(drafts),
        "published_count": sum(1 for row in rows if row["decision"].status == Decision.STATUS_PUBLISHED),
        "undecided_count": sum(1 for row in rows if row["decision"].status == Decision.STATUS_UNDECIDED),
        # 공개 취소는 전부 공개된 뒤에만 — 잘못 누른 공개를 수습하는 통로다.
        "can_unpublish": bool(decisions) and not drafts,
        "evidence_data": {
            str(row["decision"].id): _review_evidence_spans(meeting, row["decision"], row["interpretations"])
            for row in rows
        },
        # 보고 있던 문항 위치를 기억하는 브라우저 저장 키 — 저장·공개 뒤 같은 문항으로 돌아온다.
        "step_key": "decision-step-review-%d" % meeting.id,
        "choice_candidate_prefix": decision_compiler.CHOICE_CANDIDATE_PREFIX,
        "choice_custom": decision_compiler.CHOICE_CUSTOM,
        "choice_undecided": decision_compiler.CHOICE_UNDECIDED,
        "choice_drop": decision_compiler.CHOICE_DROP,
        "editable": all(row["decision"].status == Decision.STATUS_DRAFT for row in rows),
        "status_draft": Decision.STATUS_DRAFT,
        "status_published": Decision.STATUS_PUBLISHED,
        "status_undecided": Decision.STATUS_UNDECIDED,
    })


def _evidence_context(decision, interpretations):
    """같은 근거 문장이 원문에 여러 번 나올 때 어느 자리인지 가리는 데 쓰는 그 결정의 내용."""
    return " ".join([decision.what, decision.final_text] + [interpretation.text for interpretation in interpretations])


def _review_evidence_spans(meeting, decision, interpretations):
    """진행자 화면의 녹취 원문 창에서 하이라이트할 구간 — 후보마다의 근거와 명확한 결정의 근거."""
    evidences = [("후보 %s" % interpretation.label, interpretation.evidence) for interpretation in interpretations]
    evidences.append(("근거", decision.evidence))
    return decision_compiler.evidence_spans(meeting.transcript, evidences, _evidence_context(decision, interpretations))


def _published_evidence_spans(meeting, decision):
    """팀원 화면의 하이라이트 구간 — 공개된 문장의 근거.

    진행자가 후보를 골랐으면 그 후보의 근거, 명확한 결정이면 저장된 근거를 쓴다. 진행자가 직접
    쓴 문장처럼 딱 맞는 근거가 없으면 후보들의 근거를 모두 보여준다(그 결정이 나온 대목이다).
    """
    interpretations = list(decision.interpretations.all())
    chosen = [i for i in interpretations if i.text == decision.final_text and i.evidence]
    if chosen:
        evidences = [("", chosen[0].evidence)]
    elif decision.evidence:
        evidences = [("", decision.evidence)]
    else:
        evidences = [("", interpretation.evidence) for interpretation in interpretations]
    return decision_compiler.evidence_spans(meeting.transcript, evidences, _evidence_context(decision, interpretations))


@login_required
def decision_ack(request, meeting_id):
    """팀원 화면. 진행자가 공개한 확정 문장을 한 문항씩 읽고, 근거가 된 녹취 원문을 볼 수 있다.

    읽기 전용이다 — 확인 버튼은 없다. 이 화면을 열면 이 회의의 결정 알림은 읽은 것으로 보고 지운다.
    진행자는 진행자 화면으로 보낸다. 공개 전(draft) 항목은 published_decisions()가 아예 돌려주지
    않으므로 응답에 담기지 않는다.
    """
    meeting = _decision_meeting(request, meeting_id)
    if meeting.host_id == request.user.id:
        return redirect("decision_review", meeting_id=meeting.id)
    notifications.mark_read(request.user, meeting)
    decisions = decision_compiler.published_decisions(meeting)
    return render(request, "meetalign/decision_ack.html", {
        "meeting": meeting,
        "rows": [{"decision": decision} for decision in decisions],
        "evidence_data": {
            str(decision.id): _published_evidence_spans(meeting, decision) for decision in decisions
        },
        "step_key": "decision-step-ack-%d" % meeting.id,
        "status_published": Decision.STATUS_PUBLISHED,
    })


@login_required
def notification_list(request):
    """상단 메뉴 알림 아이콘이 여는 알림 목록. 읽은 알림은 지워지므로 여기 보이는 것은 모두 새 알림이다.

    '모두 읽음'은 그 사람의 알림을 한 번에 지운다.
    """
    if request.method == "POST" and request.POST.get("action") == "read_all":
        notifications.clear_all(request.user)
        return redirect("notification_list")
    items = list(
        Notification.objects.filter(user=request.user, read_at__isnull=True, meeting__team__members=request.user)
        .select_related("meeting", "meeting__team")[:50]
    )
    return render(request, "meetalign/notifications.html", {"items": items})
