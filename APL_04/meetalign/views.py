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

from . import services, tasks
from .models import Answer, ChatMessage, CheckQuestion, Meeting, Question, Recording, Team

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


@login_required
def meeting_list(request, team_id):
    team = _team(request, team_id)
    if request.method == "POST" and request.POST.get("action") == "delete_team":
        if team.created_by_id != request.user.id:
            raise PermissionDenied
        team.delete()
        return redirect("index")
    return render(request, "meetalign/meeting_list.html", {"team": team, "meetings": team.meetings.all()})


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
            return render(request, "meetalign/meeting_detail.html", {"meeting": meeting, "error": error})
        if recording:
            added = Recording.objects.create(meeting=meeting, file=recording, status=Recording.STATUS_PROCESSING)
            tasks.start_processing(added.id)
        if request.POST.get("action") == "end":
            Meeting.objects.filter(pk=meeting.pk).update(ended=True)
            if meeting.host_id == request.user.id:
                return redirect("question_review", meeting_id=meeting.id)
            return redirect("understanding", meeting_id=meeting.id)
        return redirect("meeting_result", meeting_id=meeting.id)
    return render(request, "meetalign/meeting_detail.html", {"meeting": meeting})


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


@login_required
def question_review(request, meeting_id):
    """주최자가 AI가 낸 객관식 문제를 검토·수정하고, 준비되면 참가자에게 공개한다."""
    meeting = _meeting(request, meeting_id)
    _require_host(meeting, request.user)
    if meeting.transcript and not meeting.check_questions.exists():
        for order, q in enumerate(services.generate_check_questions(meeting)):
            try:
                with transaction.atomic():
                    CheckQuestion.objects.create(
                        meeting=meeting, text=q["text"], choices=q["choices"],
                        correct_choice=q["correct_choice"], evidence=q.get("evidence", ""), order=order,
                    )
            except IntegrityError:
                break
    if request.method == "POST":
        if request.POST.get("action") == "publish":
            meeting.check_questions.filter(status=CheckQuestion.STATUS_DRAFT).update(status=CheckQuestion.STATUS_PUBLISHED)
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
    return render(request, "meetalign/question_review.html", {
        "meeting": meeting, "questions": questions, "all_published": all_published,
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
    return render(request, "meetalign/understanding.html", {"meeting": meeting, "rows": rows})


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
