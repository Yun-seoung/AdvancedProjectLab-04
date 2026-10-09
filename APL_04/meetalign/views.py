import os
from uuid import uuid4

from django.conf import settings
from django.contrib.auth import login as auth_login
from django.contrib.auth import logout as auth_logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render

from .models import AnonQuestion, Meeting, QuizItem, Response, Team
from .services import llm, stt


# ---------------------------------------------------------------------------
# 공통 도우미
# ---------------------------------------------------------------------------

def _meeting(request, meeting_id):
    """팀원만 회의에 접근할 수 있다."""
    return get_object_or_404(Meeting, pk=meeting_id, team__members=request.user)


def _hosted_meeting(request, meeting_id):
    """진행자 전용 기능의 최소 권한 체크."""
    meeting = _meeting(request, meeting_id)
    if meeting.host_id != request.user.id:
        raise PermissionDenied("진행자만 쓸 수 있는 기능입니다.")
    return meeting


def _team(request, team_id):
    return get_object_or_404(Team, pk=team_id, members=request.user)


def _transcribe_upload(upload):
    """업로드된 녹음을 임시 저장해 전사하고, 원본은 처리 후 바로 삭제한다."""
    os.makedirs(settings.MEDIA_ROOT, exist_ok=True)
    suffix = os.path.splitext(upload.name)[1][:10]
    temp_path = os.path.join(settings.MEDIA_ROOT, "upload-%s%s" % (uuid4().hex, suffix))
    try:
        with open(temp_path, "wb") as fp:
            for chunk in upload.chunks():
                fp.write(chunk)
        return stt.transcribe(temp_path)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def _rebuild_result(meeting, transcript):
    """전사 텍스트로 통합 회의록과 확인 문제를 다시 만든다 (요청 안에서 동기 처리)."""
    minutes = llm.make_minutes(transcript, meeting.title)
    items = llm.make_quiz(transcript, minutes)

    meeting.transcript = transcript
    meeting.minutes = minutes
    meeting.quiz_published = False
    meeting.save()

    Response.objects.filter(item__meeting=meeting).delete()
    meeting.quiz_items.all().delete()
    for order, item in enumerate(items, start=1):
        QuizItem.objects.create(
            meeting=meeting,
            order=order,
            kind=item["kind"],
            text=item["text"],
            choice1=item["choices"][0],
            choice2=item["choices"][1],
            choice3=item["choices"][2],
            choice4=item["choices"][3],
            answer_index=item["answer_index"],
            evidence=item["evidence"],
        )


# ---------------------------------------------------------------------------
# 로그인 / 홈
# ---------------------------------------------------------------------------

def index(request):
    teams = request.user.teams.all() if request.user.is_authenticated else []
    return render(request, "meetalign/index.html", {"teams": teams})


def login_view(request):
    form = AuthenticationForm(request, data=request.POST or None)
    if request.method == "POST" and form.is_valid():
        auth_login(request, form.get_user())
        return redirect("index")
    return render(request, "meetalign/login.html", {"form": form})


def logout_view(request):
    if request.method == "POST":
        auth_logout(request)
    return redirect("index")


def signup(request):
    """목업에서 이어받은 화면. 프로토타입은 가입을 구현하지 않고 seed 계정을 쓴다."""
    return render(request, "meetalign/signup.html")


@login_required
def team_join(request):
    """목업에서 이어받은 화면. 프로토타입은 seed 가 만든 단일 팀을 쓴다."""
    error = ""
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        team = Team.objects.filter(name=name).first()
        if team is None:
            error = "해당 이름의 팀이 없습니다."
        else:
            team.members.add(request.user)
            return redirect("meeting_list", team_id=team.id)
    return render(request, "meetalign/team_join.html", {"error": error})


# ---------------------------------------------------------------------------
# 회의
# ---------------------------------------------------------------------------

@login_required
def meeting_list(request, team_id):
    team = _team(request, team_id)
    return render(request, "meetalign/meeting_list.html",
                  {"team": team, "meetings": team.meetings.all()})


@login_required
def meeting_create(request, team_id):
    team = _team(request, team_id)
    if request.method == "POST" and request.POST.get("title", "").strip():
        meeting = Meeting.objects.create(
            team=team, title=request.POST["title"].strip(), host=request.user)
        return redirect("meeting_detail", meeting_id=meeting.id)
    return render(request, "meetalign/meeting_create.html", {"team": team})


@login_required
def meeting_detail(request, meeting_id):
    meeting = _meeting(request, meeting_id)
    error = ""

    if request.method == "POST":
        if meeting.host_id != request.user.id:
            raise PermissionDenied("회의 녹음 제출과 종료는 진행자만 할 수 있습니다.")

        if request.POST.get("action") == "end":
            meeting.ended = True
            meeting.save()
            return redirect("answers", meeting_id=meeting.id)

        transcript = (request.POST.get("transcript") or "").strip()
        upload = request.FILES.get("recording")
        try:
            if upload:
                transcript = _transcribe_upload(upload).strip()
            if not transcript:
                error = "오디오 파일을 올리거나 전사 텍스트를 붙여넣어 주세요."
            else:
                _rebuild_result(meeting, transcript)
                return redirect("meeting_result", meeting_id=meeting.id)
        except (stt.SttError, llm.LlmError) as exc:
            error = str(exc)

    return render(request, "meetalign/meeting_detail.html", {
        "meeting": meeting,
        "is_host": meeting.host_id == request.user.id,
        "error": error,
        "stt_provider": stt.provider_name(),
        "llm_provider": llm.provider_name(),
    })


@login_required
def meeting_result(request, meeting_id):
    meeting = _meeting(request, meeting_id)
    return render(request, "meetalign/meeting_result.html", {
        "meeting": meeting,
        "is_host": meeting.host_id == request.user.id,
    })


@login_required
def summary(request, meeting_id):
    meeting = _meeting(request, meeting_id)
    return render(request, "meetalign/summary.html", {"meeting": meeting})


@login_required
def minutes_download(request, meeting_id):
    meeting = _meeting(request, meeting_id)
    if not meeting.minutes:
        raise PermissionDenied("아직 통합 회의록이 없습니다.")
    response = HttpResponse(meeting.minutes, content_type="text/markdown; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="meeting-%d.md"' % meeting.id
    return response


# ---------------------------------------------------------------------------
# 확인 문제
# ---------------------------------------------------------------------------

@login_required
def quiz_review(request, meeting_id):
    """진행자가 문제와 정답을 검토, 수정, 승인(공개)한다."""
    meeting = _hosted_meeting(request, meeting_id)
    items = list(meeting.quiz_items.all())
    message = ""

    if request.method == "POST":
        for item in items:
            item.text = request.POST.get("text_%d" % item.id, item.text).strip()
            for n in range(1, 5):
                field = "choice%d" % n
                posted = request.POST.get("%s_%d" % (field, item.id), getattr(item, field))
                setattr(item, field, posted.strip())
            try:
                answer = int(request.POST.get("answer_%d" % item.id, item.answer_index))
            except ValueError:
                answer = item.answer_index
            item.answer_index = answer if answer in (1, 2, 3, 4) else item.answer_index
            item.evidence = request.POST.get("evidence_%d" % item.id, item.evidence).strip()
            item.save()

        if request.POST.get("action") == "publish":
            meeting.quiz_published = True
            meeting.save()
            return redirect("quiz_stats", meeting_id=meeting.id)
        message = "수정 내용을 저장했습니다."

    return render(request, "meetalign/quiz_review.html",
                  {"meeting": meeting, "items": items, "message": message})


@login_required
def understanding(request, meeting_id):
    """참가자가 확인 문제를 풀면 자동 채점한다."""
    meeting = _meeting(request, meeting_id)
    items = list(meeting.quiz_items.all())
    mine = {r.item_id: r for r in Response.objects.filter(item__meeting=meeting, user=request.user)}

    if request.method == "POST" and meeting.quiz_published and not mine:
        for item in items:
            try:
                selected = int(request.POST.get("item_%d" % item.id, 0))
            except ValueError:
                selected = 0
            if selected in (1, 2, 3, 4):
                Response.objects.create(
                    item=item, user=request.user, selected_index=selected,
                    is_correct=selected == item.answer_index)
        return redirect("understanding", meeting_id=meeting.id)

    graded = []
    for item in items:
        response = mine.get(item.id)
        if response is None:
            continue
        graded.append({
            "item": item,
            "selected": response.selected_index,
            "selected_text": dict(item.choices).get(response.selected_index, ""),
            "is_correct": response.is_correct,
        })

    return render(request, "meetalign/understanding.html", {
        "meeting": meeting,
        "items": items,
        "graded": graded,
        "solved": bool(mine),
        "correct_count": sum(1 for g in graded if g["is_correct"]),
    })


@login_required
def quiz_stats(request, meeting_id):
    """문항별로 '몇 명이 다르게 이해했는지'만 보여준다 (개인 이름은 노출하지 않는다)."""
    meeting = _hosted_meeting(request, meeting_id)
    rows = []
    for item in meeting.quiz_items.all():
        responses = list(item.responses.all())
        counts = {n: 0 for n in (1, 2, 3, 4)}
        for response in responses:
            counts[response.selected_index] = counts.get(response.selected_index, 0) + 1
        rows.append({
            "item": item,
            "total": len(responses),
            "different": sum(1 for r in responses if not r.is_correct),
            "choices": [{"index": n, "text": text, "count": counts.get(n, 0),
                         "is_answer": n == item.answer_index}
                        for n, text in item.choices],
        })
    return render(request, "meetalign/quiz_stats.html", {"meeting": meeting, "rows": rows})


# ---------------------------------------------------------------------------
# 질문 (Phase 3 에서 구현)
# ---------------------------------------------------------------------------

@login_required
def chat(request, meeting_id):
    meeting = _meeting(request, meeting_id)
    mode = request.POST.get("mode") or request.GET.get("mode", "llm")
    reply = ""
    if request.method == "POST" and request.POST.get("text", "").strip():
        text = request.POST["text"].strip()
        if mode == "anon":
            AnonQuestion.objects.create(meeting=meeting, text=text)
        else:
            reply = "(Phase 3 에서 구현) AI에게 묻기는 아직 연결되지 않았습니다."
    return render(request, "meetalign/chat.html",
                  {"meeting": meeting, "mode": mode, "reply": reply})


@login_required
def inbox(request, meeting_id):
    meeting = _meeting(request, meeting_id)
    return render(request, "meetalign/inbox.html",
                  {"meeting": meeting, "questions": meeting.questions.all()})


@login_required
def answers(request, meeting_id):
    meeting = _meeting(request, meeting_id)
    if request.method == "POST":
        question = get_object_or_404(AnonQuestion, pk=request.POST.get("question_id"), meeting=meeting)
        question.answer = request.POST.get("answer", "").strip()
        question.save()
        return redirect("answers", meeting_id=meeting.id)
    return render(request, "meetalign/answers.html",
                  {"meeting": meeting, "questions": meeting.questions.all()})
