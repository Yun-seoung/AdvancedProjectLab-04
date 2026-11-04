import os

from django.contrib.auth import login, logout
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.http import FileResponse, Http404, HttpResponse
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.http import url_has_allowed_host_and_scheme

from . import decision_compiler, decision_status, quiz_status, services, tasks
from .models import (
    Answer, ChatMessage, CheckQuestion, Decision, DecisionAgreement, Interpretation, Meeting,
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


def _meeting_rows(meetings):
    """회의 목록에 뿌릴 (회의 + 결정 확인 미완료 배지) 묶음.

    팀 홈은 팀원 전원이 보는 화면이라 숫자만 담고 이름은 절대 담지 않는다.
    집계는 회의 개수와 무관한 고정 쿼리 수로 한 번에 처리한다(N+1 금지).
    """
    pending_counts = decision_status.pending_count_for_meetings(meetings)
    rows = []
    for meeting in meetings:
        pending = pending_counts.get(meeting.id)
        rows.append({
            "meeting": meeting,
            # 확정 명세가 공개되지 않은 회의(처리 중, 진행자 검토 전, 전부 미결정)는
            # 배지 자체를 숨긴다.
            "decision_open": pending is not None,
            "pending": pending,
            "badge": decision_status.badge_text(pending) if pending else "",
            "tooltip": "아직 %d명이 결정을 확인하지 않았어요" % pending if pending else "",
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
        "team": team, "meetings": meetings, "rows": _meeting_rows(meetings),
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


@login_required
def meeting_create(request, team_id):
    team = _team(request, team_id)
    error = None
    if request.method == "POST" and request.POST.get("title", "").strip():
        deadline, error = _parse_deadline(request.POST.get("deadline", "").strip())
        if error is None:
            meeting = Meeting.objects.create(team=team, title=request.POST["title"].strip(), deadline=deadline, host=request.user)
            return redirect("meeting_detail", meeting_id=meeting.id)
    return render(request, "meetalign/meeting_create.html", {"team": team, "error": error})


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
        if recording and meeting.ended:
            error = "종료된 회의에는 녹음을 올릴 수 없습니다."
        elif recording:
            error = _recording_error(recording)
        if error:
            return render(request, "meetalign/meeting_detail.html", {"meeting": meeting, "error": error, "stt_status": services.stt_status_text()})
        if recording:
            added = Recording.objects.create(meeting=meeting, file=recording, status=Recording.STATUS_PROCESSING)
            tasks.start_processing(added.id)
        if request.POST.get("action") == "end":
            Meeting.objects.filter(pk=meeting.pk).update(ended=True)
            return redirect("meeting_detail", meeting_id=meeting.id)
        return redirect("meeting_result", meeting_id=meeting.id)
    return render(request, "meetalign/meeting_detail.html", {
        "meeting": meeting, "stt_status": services.stt_status_text(),
        # 결정 컴파일러(실험): 지난 회의의 미결정 안건. 읽기만 하므로 위 POST 흐름은 건드리지 않는다.
        # '처리됨' 버튼은 별도 URL(decision_carryover_resolve)로 보낸다.
        "carryover_decisions": decision_compiler.carryover_decisions(meeting),
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


def _find_discrepancies(meeting):
    found = []
    for question in meeting.check_questions.filter(status=CheckQuestion.STATUS_PUBLISHED):
        answers = list(question.answers.select_related("user"))
        if answers and any(a.selected_choice != question.correct_choice for a in answers):
            rows = [{"username": a.user.username, "text": question.choices[a.selected_choice], "correct": a.is_correct} for a in answers]
            found.append((question, rows))
    return found


def _create_check_questions(meeting, generated, batch):
    for order, q in enumerate(generated):
        try:
            with transaction.atomic():
                CheckQuestion.objects.create(
                    meeting=meeting, text=q["text"], choices=q["choices"],
                    correct_choice=q["correct_choice"], evidence=q.get("evidence", ""),
                    order=order, batch=batch,
                )
        except IntegrityError:
            break


@login_required
def question_review(request, meeting_id):
    """주최자가 AI가 낸 객관식 문제를 검토·수정하고, 준비되면 참가자에게 공개한다."""
    meeting = _meeting(request, meeting_id)
    _require_host(meeting, request.user)
    if meeting.transcript and not meeting.check_questions.exists():
        _create_check_questions(meeting, services.generate_check_questions(meeting), batch=0)
        Meeting.objects.filter(pk=meeting.pk).update(check_questions_transcript=meeting.transcript)
        meeting.check_questions_transcript = meeting.transcript
    if request.method == "POST":
        if request.POST.get("action") == "publish":
            meeting.check_questions.filter(status=CheckQuestion.STATUS_DRAFT).update(status=CheckQuestion.STATUS_PUBLISHED)
        elif request.POST.get("action") == "generate_more":
            new_text = meeting.transcript[len(meeting.check_questions_transcript):].strip()
            last = meeting.check_questions.order_by("-batch").first()
            next_batch = (last.batch + 1) if last else 0
            _create_check_questions(meeting, services.generate_check_questions_from_text(new_text), batch=next_batch)
            Meeting.objects.filter(pk=meeting.pk).update(check_questions_transcript=meeting.transcript)
        else:
            for question in meeting.check_questions.all():
                text = request.POST.get("text_%d" % question.id, "").strip()
                choices = [request.POST.get("choice_%d_%d" % (question.id, i), "").strip() for i in range(4)]
                evidence = request.POST.get("evidence_%d" % question.id, "").strip()
                try:
                    correct_choice = int(request.POST.get("correct_choice_%d" % question.id, question.correct_choice))
                except ValueError:
                    correct_choice = question.correct_choice
                if text and all(choices) and 0 <= correct_choice < 4:
                    question.text = text
                    question.choices = choices
                    question.correct_choice = correct_choice
                    question.evidence = evidence
                    question.save()
        return redirect("question_review", meeting_id=meeting.id)
    questions = list(meeting.check_questions.all())
    all_published = bool(questions) and all(q.status == CheckQuestion.STATUS_PUBLISHED for q in questions)
    has_new_transcript = bool(
        meeting.check_questions_transcript and meeting.transcript != meeting.check_questions_transcript
    )
    # 미완료 참가자 이름은 이 화면(진행자 전용)에서만 모은다. 위 _require_host를 통과한
    # 뒤에만 조회하므로, 참가자 요청에는 이름이 담긴 HTML이 애초에 만들어지지 않는다.
    quiz_open = any(q.status == CheckQuestion.STATUS_PUBLISHED for q in questions)
    pending = quiz_status.pending_participants(meeting) if quiz_open else []
    participant_total = quiz_status.participant_count(meeting) if quiz_open else 0
    participant_done = participant_total - len(pending)
    return render(request, "meetalign/question_review.html", {
        "meeting": meeting, "questions": questions, "all_published": all_published,
        "has_new_transcript": has_new_transcript,
        "quiz_open": quiz_open,
        # 이름은 '완료 여부'에만 연결한다 — 누가 어떤 보기를 골랐는지는 담지 않는다.
        "pending_rows": [{"name": user.username, "answered": done, "total": total} for user, done, total in pending],
        "participant_total": participant_total,
        "participant_done": participant_done,
        "participant_pct": round(participant_done * 100 / participant_total) if participant_total else 0,
    })


@login_required
def understanding(request, meeting_id):
    meeting = _meeting(request, meeting_id)
    user = request.user
    questions = list(meeting.check_questions.filter(status=CheckQuestion.STATUS_PUBLISHED))
    if request.method == "POST":
        for question in questions:
            raw = request.POST.get("q%d" % question.id)
            if raw is None or raw == "":
                continue
            try:
                selected = int(raw)
            except ValueError:
                continue
            if 0 <= selected < len(question.choices):
                Answer.objects.update_or_create(question=question, user=user, defaults={"selected_choice": selected})
        return redirect("understanding", meeting_id=meeting.id)
    my_answers = {a.question_id: a.selected_choice for a in Answer.objects.filter(question__meeting=meeting, user=user)}
    rows = [(q, my_answers.get(q.id)) for q in questions]
    # 한 문항도 안 고르고 저장해도 넘어가기 때문에(위 continue), 본인은 다 answered고 생각하는데
    # 실제로는 남아 있는 경우가 생긴다. 녹음을 추가해 batch가 늘어난 경우도 마찬가지라서,
    # 몇 문항이 남았는지 당사자 화면에서 바로 보이게 한다.
    answered_count = sum(1 for _, selected in rows if selected is not None)
    total_count = len(rows)
    return render(request, "meetalign/understanding.html", {
        "meeting": meeting, "rows": rows,
        "answered_count": answered_count,
        "total_count": total_count,
        "remaining_count": total_count - answered_count,
        "progress_pct": round(answered_count * 100 / total_count) if total_count else 0,
        # 한 번도 답한 적 없으면 문항마다 '미응답'을 달아봐야 시끄럽기만 하다.
        "started": answered_count > 0,
    })


@login_required
def verification(request, meeting_id):
    meeting = _meeting(request, meeting_id)
    members = list(meeting.team.members.all())
    questions = list(meeting.check_questions.filter(status=CheckQuestion.STATUS_PUBLISHED))
    pending = []
    answered_slots = 0
    for question in questions:
        answered = set(question.answers.values_list("user_id", flat=True))
        missing = [member for member in members if member.id not in answered]
        answered_slots += len(members) - len(missing)
        if missing:
            pending.append((question, missing))
    total_slots = len(members) * len(questions)
    return render(request, "meetalign/verification.html", {
        "meeting": meeting,
        "discrepancies": _find_discrepancies(meeting),
        "pending": pending,
        "answered_slots": answered_slots,
        "total_slots": total_slots,
        "progress_pct": round(answered_slots * 100 / total_slots) if total_slots else 0,
    })


@login_required
def record_file(request, meeting_id):
    meeting = _meeting(request, meeting_id)
    if not meeting.record:
        raise Http404
    response = HttpResponse(meeting.record, content_type="text/plain; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="meeting_record.txt"'
    return response


# ---------------------------------------------------------------------------
# 결정 컴파일러 (실험 기능). 기존 이해도 퀴즈 화면(question_review/understanding/
# verification)은 그대로 두고, 그 옆에 별도 흐름으로 붙인다.
# ---------------------------------------------------------------------------


def _decision_meeting(request, meeting_id):
    """결정 컴파일러를 쓸 수 있는 회의만 통과시킨다.

    팀 밖 사용자는 _meeting()이 404로 막고, 회의록이 아직 완성되지 않은 회의는 여기서 404다
    — 컴파일할 전사 원문이 없는 회의에 링크를 노출하지 않기 위함(템플릿 쪽도 같은 조건).
    """
    meeting = _meeting(request, meeting_id)
    if meeting.processing_status != Meeting.STATUS_DONE:
        raise Http404
    return meeting


def _reset_decisions(meeting):
    """컴파일 결과를 전부 지운다. 공개된 뒤에는 거부한다.

    팀원이 이미 확인한 내용이 사라지면 안 되기 때문이다. 공개를 되돌리려면 먼저 '공개 취소'로
    draft로 내려야 한다(확인 기록이 하나도 없을 때만 가능).
    """
    if meeting.decisions.exclude(status=Decision.STATUS_DRAFT).exists():
        return "이미 공개된 항목이 있어 명세를 지울 수 없습니다. '공개 취소'를 먼저 눌러 주세요."
    meeting.decisions.all().delete()
    return None


def _unpublish_decisions(meeting):
    """공개를 되돌려 draft로 내린다 — 공개 직후 "잘못 눌렀다"를 수습하는 통로.

    팀원이 한 명이라도 확인을 남겼으면 거부한다. 그 뒤에 명세가 바뀌면 팀원이 확인한 내용과
    달라지는데, 그건 공개 취소가 아니라 '확정 문장 수정'(revise)이 다룰 일이다.
    """
    published = meeting.decisions.exclude(status=Decision.STATUS_DRAFT)
    if not published.exists():
        return "공개된 항목이 없습니다."
    if DecisionAgreement.objects.filter(decision__meeting=meeting).exists():
        return "팀원이 이미 확인한 항목이 있어 공개를 취소할 수 없습니다. 내용을 고치려면 '확정 문장 수정'을 쓰세요."
    published.update(status=Decision.STATUS_DRAFT, revised=False)
    return None


def _recompute_warnings(meeting, decisions):
    """진행자가 빈칸을 채우거나 모호어를 고치면 경고도 같이 사라져야 한다.

    같은 담당자에게 걸린 날짜 충돌과 "회의당 최대 5개" 제한은 항목 하나만 봐서는 판정할 수
    없으므로 회의 전체를 한 번에 다시 센다. 단, 저장은 아직 draft인 항목에만 한다 — 투표가
    시작된 항목의 경고가 바뀌면 참가자가 본 화면과 달라진다.
    """
    specs = [
        {"who": d.who, "what": d.what, "due": d.due_text, "done": d.done_criteria, "state": d.confirm_state}
        for d in decisions
    ]
    for decision, warnings in zip(decisions, decision_compiler.warnings_for(specs)):
        if decision.status == Decision.STATUS_DRAFT and decision.warnings != warnings:
            decision.warnings = warnings
            decision.save(update_fields=["warnings"])


def _apply_host_choice(request, decision):
    """경고 항목에 대한 진행자의 확정 선택 하나를 적용한다.

    선택을 보내지 않은 항목은 손대지 않는다 — 한 항목을 고치려고 저장했다가 다른 항목의
    확정이 날아가면 안 된다. 실패 사유 문자열 또는 None을 돌려준다.
    """
    raw = request.POST.get("choice_%d" % decision.id, "")
    if not raw:
        return None

    if raw == decision_compiler.CHOICE_UNDECIDED:
        decision.final_text = ""
        decision.marked_undecided = True
    elif raw == decision_compiler.CHOICE_CUSTOM:
        text = request.POST.get("custom_text_%d" % decision.id, "").strip()
        if not text:
            return "'직접 작성'을 골랐으면 확정 문장을 한 줄 적어 주세요."
        decision.final_text = text
        decision.marked_undecided = False
    elif raw.startswith(decision_compiler.CHOICE_CANDIDATE_PREFIX):
        interpretation = get_object_or_404(
            Interpretation,
            pk=raw[len(decision_compiler.CHOICE_CANDIDATE_PREFIX):], decision=decision,
        )
        # 고른 시점의 문장을 복사해 둔다. 뒤에 후보 문장을 고쳐도 확정 내용은 따라 바뀌지 않는다.
        decision.final_text = interpretation.text
        decision.marked_undecided = False
    else:
        return "확정 방법을 하나 골라 주세요."

    decision.save(update_fields=["final_text", "marked_undecided"])
    return None


def _save_decision_edits(request, meeting):
    """진행자가 실행 명세·해석 후보·확정 선택을 고친 것을 저장한다. draft 항목만 대상이다.

    실패 사유 문자열 또는 None을 돌려준다. 명세 수정은 항목별로 독립이라 한 항목이 비어
    있어도 건너뛰고 계속하지만, 확정 선택이 잘못된 경우는 진행자가 알아야 하므로 사유를 올린다.
    """
    decisions = list(meeting.decisions.prefetch_related("interpretations"))
    valid_states = {choice for choice, _ in Decision.CONFIRM_CHOICES}
    error = None
    for decision in decisions:
        if decision.status != Decision.STATUS_DRAFT:
            continue
        what = request.POST.get("what_%d" % decision.id, "").strip()
        if what:
            state = request.POST.get("confirm_state_%d" % decision.id, decision.confirm_state)
            decision.who = request.POST.get("who_%d" % decision.id, "").strip()
            decision.what = what
            decision.due_text = request.POST.get("due_%d" % decision.id, "").strip()
            decision.done_criteria = request.POST.get("done_%d" % decision.id, "").strip()
            decision.confirm_state = state if state in valid_states else decision.confirm_state
            decision.save(update_fields=["who", "what", "due_text", "done_criteria", "confirm_state"])
        # '무엇을'이 비어 있으면 실행 명세가 아니다 — 그 항목의 명세만 건너뛰고 확정 선택은 그대로 받는다.

        for interpretation in decision.interpretations.all():
            if request.POST.get("drop_interpretation_%d" % interpretation.id):
                interpretation.delete()
                continue
            text = request.POST.get("interpretation_%d" % interpretation.id, "").strip()
            if text and text != interpretation.text:
                interpretation.text = text
                interpretation.save(update_fields=["text"])

        failed = _apply_host_choice(request, decision)
        if failed and error is None:
            error = failed
    _recompute_warnings(meeting, list(meeting.decisions.all()))
    return error


def _publish_decisions(meeting):
    """"확정 명세 공개" — draft 항목을 팀원에게 공개한다.

    경고가 붙었는데 진행자가 아직 고르지 않은 항목이 하나라도 있으면 거부한다(화면의 버튼도
    같은 조건으로 비활성화된다).
    """
    drafts = list(meeting.decisions.filter(status=Decision.STATUS_DRAFT))
    if not drafts:
        return "공개할 항목이 없습니다."
    blockers = decision_compiler.publish_blockers(drafts)
    if blockers:
        return "아직 확정하지 않은 항목이 %d개 있습니다. 후보를 고르거나 직접 작성하거나 '아직 미정'으로 넘겨 주세요." % len(blockers)
    decision_compiler.publish_decisions(meeting)
    return None


def _revise_final_text(request, meeting):
    """공개된 항목의 확정 문장을 고친다. 그 항목의 확인 기록은 모두 지워지고 다시 받는다."""
    decision = get_object_or_404(
        Decision, pk=request.POST.get("decision_id"), meeting=meeting,
        status=Decision.STATUS_PUBLISHED,
    )
    text = request.POST.get("final_text", "").strip()
    if not text:
        return "확정 문장을 한 줄 적어 주세요."
    if text == decision.final_text:
        # 같은 문장으로 저장하면 팀원 확인만 날아가므로 아무것도 하지 않는다.
        return None
    decision_compiler.revise_final_text(decision, text)
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
    if not decision.final_text:
        return "", ""
    for interpretation in interpretations:
        if interpretation.text == decision.final_text:
            return "%s%d" % (decision_compiler.CHOICE_CANDIDATE_PREFIX, interpretation.id), ""
    return decision_compiler.CHOICE_CUSTOM, decision.final_text


@login_required
def decision_review(request, meeting_id):
    """진행자 전용. 컴파일 → 명세·경고 확인과 수정 → 항목별 확정 → "확정 명세 공개"."""
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
            error = _publish_decisions(meeting)
        elif action == "unpublish":
            error = _unpublish_decisions(meeting)
        elif action == "revise":
            error = _revise_final_text(request, meeting)
        if error is None:
            return redirect("decision_review", meeting_id=meeting.id)

    decisions = list(meeting.decisions.prefetch_related("interpretations"))
    ack_counts = decision_compiler.ack_counts(decisions)
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
            # 경고가 붙었는데 아직 고르지 않은 항목 — 공개를 막는다.
            "blocking": not decision.is_settled,
            # 공개 항목의 "확인 N명 / 달랐어요 M명". 작성자 없는 숫자와 의견 문자열만 담는다.
            "ack": ack_counts.get(decision.id),
            "comments": ack_counts.get(decision.id, {}).get("comments", []),
        })
    warned = [row for row in rows if row["decision"].warnings]
    drafts = [row for row in rows if row["decision"].status == Decision.STATUS_DRAFT]
    blockers = [row for row in drafts if row["blocking"]]
    # 미확인 팀원 이름은 이 화면(진행자 전용)에서만 모은다. 함수 앞머리의 _require_host를
    # 통과한 뒤에만 조회하므로, 팀원 요청에는 이름이 담긴 HTML이 애초에 만들어지지 않는다.
    decision_open = any(row["decision"].status == Decision.STATUS_PUBLISHED for row in rows)
    pending = decision_status.pending_participants(meeting) if decision_open else []
    participant_total = decision_status.participant_count(meeting) if decision_open else 0
    participant_done = participant_total - len(pending)
    return render(request, "meetalign/decision_review.html", {
        "meeting": meeting,
        "rows": rows,
        "error": error,
        "compiled": bool(decisions),
        "warned_count": len(warned),
        "draft_count": len(drafts),
        "blocker_count": len(blockers),
        "decision_open": decision_open,
        # 이름은 '확인 여부'에만 연결한다 — 누가 '제 이해와 달랐어요'를 골랐는지는 담지 않는다.
        "pending_rows": [{"name": user.username, "acked": acked, "total": total} for user, acked, total in pending],
        "participant_total": participant_total,
        "participant_done": participant_done,
        "participant_pct": round(participant_done * 100 / participant_total) if participant_total else 0,
        # 확정하지 않은 경고 항목이 하나도 없어야 공개할 수 있다(서버도 같은 조건으로 거부).
        "can_publish": bool(drafts) and not blockers,
        "published_count": sum(1 for row in rows if row["decision"].status == Decision.STATUS_PUBLISHED),
        "undecided_count": sum(1 for row in rows if row["decision"].status == Decision.STATUS_UNDECIDED),
        # 공개 취소는 팀원 확인이 하나도 없을 때만 — 잘못 누른 공개를 수습하는 통로다.
        "can_unpublish": (
            bool(decisions) and not drafts
            and not DecisionAgreement.objects.filter(decision__meeting=meeting).exists()
        ),
        "choice_candidate_prefix": decision_compiler.CHOICE_CANDIDATE_PREFIX,
        "choice_custom": decision_compiler.CHOICE_CUSTOM,
        "choice_undecided": decision_compiler.CHOICE_UNDECIDED,
        "max_warnings": decision_compiler.MAX_WARNED_DECISIONS,
        "min_interpretations": decision_compiler.MIN_INTERPRETATIONS,
        "editable": all(row["decision"].status == Decision.STATUS_DRAFT for row in rows),
        "confirm_choices": Decision.CONFIRM_CHOICES,
        "status_draft": Decision.STATUS_DRAFT,
        "status_published": Decision.STATUS_PUBLISHED,
        "status_undecided": Decision.STATUS_UNDECIDED,
    })


ACK_COMMENT_MAX = 200


def _save_ack(request, meeting):
    """팀원 한 명의 확인 하나를 저장한다. 공개 항목만 대상이고 미결정 항목은 읽기 전용이다.

    다시 누르면 덮어쓴다(진행자 집계는 항상 현재 선택 기준). 의견은 '달랐어요'에만 붙는다.
    """
    decision = get_object_or_404(
        Decision, pk=request.POST.get("decision_id"), meeting=meeting, status=Decision.STATUS_PUBLISHED,
    )
    kind = request.POST.get("kind")
    if kind not in (DecisionAgreement.KIND_ACK, DecisionAgreement.KIND_DIFFER):
        return
    comment = " ".join(request.POST.get("comment", "").split())[:ACK_COMMENT_MAX] if kind == DecisionAgreement.KIND_DIFFER else ""
    DecisionAgreement.objects.update_or_create(
        decision=decision, user=request.user, defaults={"kind": kind, "comment": comment},
    )


@login_required
def decision_ack(request, meeting_id):
    """팀원 확인 화면. 공개된 확정 명세마다 [확인했어요] / [제 이해와 달랐어요]를 고른다.

    진행자는 확인 대상이 아니라 진행자 화면으로 보낸다. 공개 전(draft) 항목은
    published_decisions()가 아예 돌려주지 않으므로 응답에 담기지 않는다.
    """
    meeting = _decision_meeting(request, meeting_id)
    if meeting.host_id == request.user.id:
        return redirect("decision_review", meeting_id=meeting.id)
    if request.method == "POST":
        _save_ack(request, meeting)
        return redirect("decision_ack", meeting_id=meeting.id)
    decisions = decision_compiler.published_decisions(meeting)
    # 내 기록만 따로 조회한다 — 다른 팀원의 선택은 이 화면에 필요 없다.
    mine = {a.decision_id: a for a in DecisionAgreement.objects.filter(decision__meeting=meeting, user=request.user)}
    rows = [{
        "decision": decision,
        "mine": mine.get(decision.id),
        # 공개 뒤 문장이 바뀌면 기존 확인이 지워지므로, 다시 확인하기 전까지만 안내한다.
        "revised_notice": decision.revised and decision.id not in mine,
    } for decision in decisions]
    published = [row for row in rows if row["decision"].status == Decision.STATUS_PUBLISHED]
    return render(request, "meetalign/decision_ack.html", {
        "meeting": meeting,
        "rows": rows,
        "published_count": len(published),
        "acked_count": sum(1 for row in published if row["mine"]),
        "kind_ack": DecisionAgreement.KIND_ACK,
        "kind_differ": DecisionAgreement.KIND_DIFFER,
        "status_published": Decision.STATUS_PUBLISHED,
        "comment_max": ACK_COMMENT_MAX,
    })
