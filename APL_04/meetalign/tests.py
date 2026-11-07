import io
import json
import os
import re
import sys
import tempfile
import types
from unittest.mock import MagicMock, patch
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, connection, transaction
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from . import decision_compiler, services, tasks, views, whisper_worker
from .models import (
    ChatMessage, Checkpoint, Decision, DecisionAgreement,
    Interpretation, Meeting, Notification, Question, Recording, SttUsage, Team,
)

# 셸에 실행용 LLM/STT 환경변수가 설정돼 있어도 테스트가 실제 모델/API를 호출하지 않고 항상 같은 결과가
# 나오도록, 모듈 시작 시 잠시 치웠다가 끝나면 되돌린다. 필요한 테스트는 각자 patch.dict로 값을 넣는다.
_MODEL_ENV_VARS = (
    "GEMINI_API_KEY", "GEMINI_MODEL", "GEMINI_TIMEOUT", "GEMINI_COOLDOWN", "OLLAMA_MODEL", "OLLAMA_TIMEOUT", "MLX_WHISPER_MODEL",
    "FASTER_WHISPER_MODEL", "FASTER_WHISPER_DEVICE", "FASTER_WHISPER_COMPUTE_TYPE",
    "CLOVA_SPEECH_INVOKE_URL", "CLOVA_SPEECH_SECRET_KEY", "CLOVA_SPEECH_FREE_MINUTES", "CLOVA_SPEECH_DIARIZATION",
    "GROQ_API_KEY", "GROQ_MODEL", "GROQ_LLM_MODEL",
    "LOCAL_DIARIZATION", "DIARIZATION_NUM_SPEAKERS", "DIARIZATION_THRESHOLD", "DIARIZATION_MODEL_DIR",
)
_saved_model_env = {}


def setUpModule():
    for name in _MODEL_ENV_VARS:
        if name in os.environ:
            _saved_model_env[name] = os.environ.pop(name)


def tearDownModule():
    os.environ.update(_saved_model_env)
    _saved_model_env.clear()


class AuthTests(TestCase):
    def test_signup_creates_user_and_logs_in(self):
        r = self.client.post(reverse("signup"), {
            "username": "newuser", "password1": "a-str0ng-pw", "password2": "a-str0ng-pw",
        })
        self.assertRedirects(r, reverse("index"))
        self.assertTrue(User.objects.filter(username="newuser").exists())
        self.assertIn("_auth_user_id", self.client.session)

    def test_signup_rejects_mismatched_passwords(self):
        r = self.client.post(reverse("signup"), {
            "username": "newuser", "password1": "a-str0ng-pw", "password2": "different",
        })
        self.assertEqual(r.status_code, 200)
        self.assertFalse(User.objects.filter(username="newuser").exists())

    def test_login_with_correct_credentials_succeeds(self):
        User.objects.create_user("a", password="pw12345!")
        r = self.client.post(reverse("login"), {"username": "a", "password": "pw12345!"})
        self.assertRedirects(r, reverse("index"))
        self.assertIn("_auth_user_id", self.client.session)

    def test_login_with_wrong_password_fails(self):
        User.objects.create_user("a", password="pw12345!")
        r = self.client.post(reverse("login"), {"username": "a", "password": "wrong"})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_logout_clears_session(self):
        user = User.objects.create_user("a", password="pw12345!")
        self.client.force_login(user)
        self.client.get(reverse("logout"))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_protected_pages_redirect_to_login(self):
        team = Team.objects.create(name="t")
        r = self.client.get(reverse("meeting_list", args=[team.id]))
        self.assertRedirects(r, "%s?next=%s" % (reverse("login"), reverse("meeting_list", args=[team.id])))


@override_settings(BACKGROUND_RECORDING=False)


class PrototypeFlowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("a", password="pw12345!")
        self.client.force_login(self.user)
        self.team = Team.objects.create(name="t")
        self.team.members.add(self.user)
        self.meeting = Meeting.objects.create(team=self.team, title="m", host=self.user, record="회의록 내용", transcript="전사본 내용")

    def test_index_public(self):
        self.assertEqual(self.client.get(reverse("index")).status_code, 200)

    def test_team_create_and_join(self):
        self.client.post(reverse("team_join"), {"action": "create", "name": "new"})
        self.assertTrue(Team.objects.get(name="new").members.filter(pk=self.user.pk).exists())
        other_team = Team.objects.create(name="o")
        self.client.post(reverse("team_join"), {"action": "join", "code": other_team.invite_code})
        self.assertTrue(other_team.members.filter(pk=self.user.pk).exists())

    def test_join_rejects_unknown_invite_code(self):
        r = self.client.post(reverse("team_join"), {"action": "join", "code": "ZZZZZZ"})
        self.assertContains(r, "해당 초대 코드의 팀이 없습니다")

    def test_each_team_gets_unique_invite_code(self):
        a = Team.objects.create(name="a-team")
        b = Team.objects.create(name="b-team")
        self.assertNotEqual(a.invite_code, b.invite_code)
        self.assertEqual(len(a.invite_code), 6)

    def test_non_member_forbidden(self):
        meeting = Meeting.objects.create(team=Team.objects.create(name="other"), title="x")
        self.assertEqual(self.client.get(reverse("meeting_detail", args=[meeting.id])).status_code, 404)

    def test_meeting_create_and_all_screens(self):
        self.client.post(reverse("meeting_create", args=[self.team.id]), {"title": "x"})
        self.assertTrue(Meeting.objects.filter(title="x").exists())
        for name in ["meeting_list", "meeting_create"]:
            self.assertEqual(self.client.get(reverse(name, args=[self.team.id])).status_code, 200)
        for name in ["meeting_detail", "meeting_result", "chat", "inbox",
                     "summary", "answers"]:
            self.assertEqual(self.client.get(reverse(name, args=[self.meeting.id])).status_code, 200, name)

    def test_submit_and_end(self):
        url = reverse("meeting_detail", args=[self.meeting.id])
        self.assertRedirects(self.client.post(url, {"action": "submit"}),
                             reverse("meeting_result", args=[self.meeting.id]))
        self.assertRedirects(self.client.post(url, {"action": "end"}), url)
        self.meeting.refresh_from_db()
        self.assertTrue(self.meeting.ended)

    def test_old_quiz_urls_are_gone(self):
        # 앱 URL은 setting/urls.py에서 meetalign/ 아래 마운트된다. 접두사를 하드코딩하면
        # 경로가 틀려도 404가 나와 테스트가 엉뚱한 이유로 통과하므로 reverse에서 파생시킨다.
        base = reverse("meeting_detail", args=[self.meeting.id])
        for suffix in ("understanding/", "questions/review/", "verification/"):
            r = self.client.get(base + suffix)
            self.assertEqual(r.status_code, 404, suffix)

    def test_quiz_url_names_no_longer_resolve(self):
        for name in ("understanding", "question_review", "verification"):
            with self.assertRaises(NoReverseMatch, msg=name):
                reverse(name, args=[self.meeting.id])

    def test_anonymous_question_flow_survives_quiz_removal(self):
        self.client.post(reverse("chat", args=[self.meeting.id]), {"mode": "anon", "text": "질문입니다"})
        q = Question.objects.get(meeting=self.meeting)
        self.client.post(reverse("answers", args=[self.meeting.id]), {"question_id": q.id, "answer": "답변입니다"})
        q.refresh_from_db()
        self.assertEqual(q.answer, "답변입니다")
        self.assertContains(self.client.get(reverse("inbox", args=[self.meeting.id])), "답변입니다")

    def test_non_host_stays_on_the_meeting_after_end(self):
        other = User.objects.create_user("b", password="pw12345!")
        self.team.members.add(other)
        member = Client()
        member.force_login(other)
        url = reverse("meeting_detail", args=[self.meeting.id])
        self.assertRedirects(member.post(url, {"action": "end"}), url)

    @override_settings(MEDIA_ROOT=tempfile.mkdtemp())
    def test_recording_upload_is_saved(self):
        url = reverse("meeting_detail", args=[self.meeting.id])
        file = SimpleUploadedFile("test.mp3", b"fake audio bytes", content_type="audio/mpeg")
        self.client.post(url, {"action": "submit", "recording": file})
        self.meeting.refresh_from_db()
        self.assertIn("test", Recording.objects.get(meeting=self.meeting).file.name)

    def test_meeting_detail_has_inline_audio_player_for_recording(self):
        self.client.post(reverse("meeting_detail", args=[self.meeting.id]), {
            "action": "submit", "recording": SimpleUploadedFile("test.mp3", b"fake audio bytes", content_type="audio/mpeg"),
        })
        rec = Recording.objects.get(meeting=self.meeting)
        r = self.client.get(reverse("meeting_detail", args=[self.meeting.id]))
        self.assertContains(r, "<audio")
        self.assertContains(r, reverse("recording_file", args=[self.meeting.id, rec.id]))

    def test_meeting_detail_offers_mic_recording_before_upload(self):
        r = self.client.get(reverse("meeting_detail", args=[self.meeting.id]))
        self.assertContains(r, "record-toggle")
        self.assertContains(r, "getUserMedia")

    def test_mic_recording_not_offered_once_meeting_ended(self):
        self.meeting.ended = True
        self.meeting.save()
        r = self.client.get(reverse("meeting_detail", args=[self.meeting.id]))
        self.assertNotContains(r, "record-toggle")

    def test_summary_shows_saved_record(self):
        self.meeting.record = "결정: A안으로 진행"
        self.meeting.save()
        r = self.client.get(reverse("summary", args=[self.meeting.id]))
        self.assertContains(r, "결정: A안으로 진행")

    def test_summary_without_record_shows_fallback(self):
        self.meeting.record = ""
        self.meeting.save()
        r = self.client.get(reverse("summary", args=[self.meeting.id]))
        self.assertContains(r, "아직 생성된 회의록이 없습니다.")

    def test_anonymous_question_and_answer(self):
        self.client.post(reverse("chat", args=[self.meeting.id]), {"mode": "anon", "text": "why?"})
        q = Question.objects.get()
        self.client.post(reverse("answers", args=[self.meeting.id]), {"question_id": q.id, "answer": "because"})
        q.refresh_from_db()
        self.assertEqual(q.answer, "because")

    def test_llm_chat_does_not_touch_anon_questions(self):
        r = self.client.post(reverse("chat", args=[self.meeting.id]), {"mode": "llm", "text": "hi"}, follow=True)
        self.assertContains(r, "테스트용 가짜 AI")
        self.assertEqual(Question.objects.count(), 0)

    def test_llm_chat_stores_conversation_per_user(self):
        self.client.post(reverse("chat", args=[self.meeting.id]), {"mode": "llm", "text": "hi"})
        messages = list(ChatMessage.objects.filter(meeting=self.meeting, user=self.user))
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0].role, ChatMessage.ROLE_USER)
        self.assertEqual(messages[0].text, "hi")
        self.assertEqual(messages[1].role, ChatMessage.ROLE_ASSISTANT)

    def test_meeting_detail_back_link(self):
        r = self.client.get(reverse("meeting_detail", args=[self.meeting.id]))
        self.assertContains(r, 'href="%s"' % reverse("meeting_list", args=[self.team.id]))

    def test_sub_screens_have_back_to_meeting_link(self):
        back = 'href="%s"' % reverse("meeting_detail", args=[self.meeting.id])
        for name in ["meeting_result", "chat", "inbox", "summary", "answers"]:
            r = self.client.get(reverse(name, args=[self.meeting.id]))
            self.assertContains(r, back, msg_prefix=name)

    def test_d1_banner_shown_when_deadline_is_tomorrow(self):
        self.meeting.deadline = timezone.localdate() + timedelta(days=1)
        self.meeting.save()
        self.assertContains(self.client.get(reverse("meeting_detail", args=[self.meeting.id])), "마감 D-1")
        self.assertContains(self.client.get(reverse("meeting_list", args=[self.team.id])), "[D-1 마감]")

    def test_d1_banner_shown_on_every_page_not_just_index(self):
        self.meeting.deadline = timezone.localdate() + timedelta(days=1)
        self.meeting.save()
        self.assertContains(self.client.get(reverse("team_join")), "내일 마감인 회의가 있습니다")

    def test_no_banner_when_deadline_further_away(self):
        self.meeting.deadline = timezone.localdate() + timedelta(days=3)
        self.meeting.save()
        r = self.client.get(reverse("meeting_detail", args=[self.meeting.id]))
        self.assertNotContains(r, "마감 D-1")

    def test_meeting_create_saves_deadline(self):
        self.client.post(reverse("meeting_create", args=[self.team.id]), {"title": "d", "deadline": "2030-01-02"})
        self.assertEqual(str(Meeting.objects.get(title="d").deadline), "2030-01-02")

    def _member_client(self):
        other = User.objects.create_user("member", password="pw12345!")
        self.team.members.add(other)
        client = Client()
        client.force_login(other)
        return client

    def test_meeting_create_sets_host_to_creator(self):
        self.client.post(reverse("meeting_create", args=[self.team.id]), {"title": "h"})
        self.assertEqual(Meeting.objects.get(title="h").host, self.user)

    def test_non_host_can_view_inbox_but_not_answer(self):
        member = self._member_client()
        self.assertEqual(member.get(reverse("inbox", args=[self.meeting.id])).status_code, 200)
        q = Question.objects.create(meeting=self.meeting, text="why?")
        r = member.post(reverse("answers", args=[self.meeting.id]), {"question_id": q.id, "answer": "x"})
        self.assertEqual(r.status_code, 403)
        q.refresh_from_db()
        self.assertEqual(q.answer, "")

    def test_asker_sees_only_their_own_question_and_answer(self):
        member_user = User.objects.create_user("member", password="pw12345!")
        self.team.members.add(member_user)
        member = Client()
        member.force_login(member_user)

        other_user = User.objects.create_user("c", password="pw12345!")
        self.team.members.add(other_user)
        other = Client()
        other.force_login(other_user)

        Question.objects.create(meeting=self.meeting, text="내 질문", answer="주최자 답변", user=member_user)
        Question.objects.create(meeting=self.meeting, text="다른 사람 질문", answer="다른 답변", user=other_user)

        r = member.get(reverse("inbox", args=[self.meeting.id]))
        self.assertContains(r, "내 질문")
        self.assertContains(r, "주최자 답변")
        self.assertNotContains(r, "다른 사람 질문")

        r2 = other.get(reverse("inbox", args=[self.meeting.id]))
        self.assertContains(r2, "다른 사람 질문")
        self.assertNotContains(r2, "내 질문")

    def test_host_sees_all_questions_regardless_of_asker(self):
        member = self._member_client()
        member.post(reverse("chat", args=[self.meeting.id]), {"mode": "anon", "text": "팀원 질문"})
        r = self.client.get(reverse("inbox", args=[self.meeting.id]))
        self.assertContains(r, "팀원 질문")

    def test_meeting_without_host_stays_open_to_members(self):
        self.meeting.host = None
        self.meeting.save()
        member = self._member_client()
        self.assertEqual(member.get(reverse("inbox", args=[self.meeting.id])).status_code, 200)

    def test_members_can_still_send_anonymous_question(self):
        member = self._member_client()
        member.post(reverse("chat", args=[self.meeting.id]), {"mode": "anon", "text": "hi"})
        self.assertEqual(Question.objects.filter(meeting=self.meeting).count(), 1)
        self.assertEqual(Question.objects.get().user.username, "member")

    def test_inbox_link_visible_to_everyone(self):
        member = self._member_client()
        r = member.get(reverse("meeting_detail", args=[self.meeting.id]))
        self.assertContains(r, reverse("inbox", args=[self.meeting.id]))

    def test_host_sees_inbox_link(self):
        r = self.client.get(reverse("meeting_detail", args=[self.meeting.id]))
        self.assertContains(r, reverse("inbox", args=[self.meeting.id]))

    def test_chat_conversation_is_isolated_per_user(self):
        member = self._member_client()
        self.client.post(reverse("chat", args=[self.meeting.id]), {"mode": "llm", "text": "내 질문"})
        member.post(reverse("chat", args=[self.meeting.id]), {"mode": "llm", "text": "다른 사람 질문"})
        r = self.client.get(reverse("chat", args=[self.meeting.id]))
        self.assertContains(r, "내 질문")
        self.assertNotContains(r, "다른 사람 질문")

    def test_second_chat_turn_includes_first_turn_as_history(self):
        with patch.dict(os.environ, {"OLLAMA_MODEL": "m1"}), patch.object(services, "_generate", return_value="답1"):
            self.client.post(reverse("chat", args=[self.meeting.id]), {"mode": "llm", "text": "질문1"})
        with patch.dict(os.environ, {"OLLAMA_MODEL": "m1"}), patch.object(services, "chat_reply") as reply:
            reply.return_value = "답2"
            self.client.post(reverse("chat", args=[self.meeting.id]), {"mode": "llm", "text": "질문2"})
        self.assertEqual(reply.call_args.kwargs["history"], [("user", "질문1"), ("assistant", "답1")])


class LocalLLMTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("a", password="pw12345!")
        self.team = Team.objects.create(name="t")
        self.team.members.add(self.user)
        self.meeting = Meeting.objects.create(
            team=self.team, title="m", host=self.user,
            record="결정: A안으로 진행", transcript="오늘 회의에서 A안으로 진행하기로 결정했습니다.",
        )

    def test_disabled_without_model_env_keeps_fake_behavior(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIn("(테스트용 가짜 AI 응답)", services.chat_reply(self.meeting, "q"))

    def test_chat_reply_sends_record_as_context(self):
        with patch.dict(os.environ, {"OLLAMA_MODEL": "m1"}), patch.object(services, "_generate", return_value="A안입니다") as gen:
            reply = services.chat_reply(self.meeting, "무엇을 정했나?")
        self.assertEqual(reply, "A안입니다")
        self.assertIn("결정: A안으로 진행", gen.call_args[0][0])
        self.assertIn("무엇을 정했나?", gen.call_args[0][0])

    def test_chat_reply_includes_conversation_history(self):
        history = [("user", "언제까지야?"), ("assistant", "다음 주 금요일입니다.")]
        with patch.dict(os.environ, {"OLLAMA_MODEL": "m1"}), patch.object(services, "_generate", return_value="네 맞습니다") as gen:
            services.chat_reply(self.meeting, "확실해?", history=history)
        prompt = gen.call_args[0][0]
        self.assertIn("언제까지야?", prompt)
        self.assertIn("다음 주 금요일입니다.", prompt)

    def test_chat_reply_without_history_has_no_history_section(self):
        with patch.dict(os.environ, {"OLLAMA_MODEL": "m1"}), patch.object(services, "_generate", return_value="답") as gen:
            services.chat_reply(self.meeting, "q")
        self.assertNotIn("이전 대화", gen.call_args[0][0])

    def test_llm_failure_falls_back(self):
        with patch.dict(os.environ, {"OLLAMA_MODEL": "m1"}), patch.object(services, "_generate", side_effect=OSError("down")):
            self.assertIn("AI 연결 실패", services.chat_reply(self.meeting, "q"))


class LanguageFilterTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("a", password="pw12345!")
        self.team = Team.objects.create(name="t")
        self.team.members.add(self.user)
        self.meeting = Meeting.objects.create(
            team=self.team, title="m", host=self.user,
            record="결정: A안으로 진행", transcript="오늘 회의에서 A안으로 진행하기로 결정했습니다.",
        )

    def test_allows_korean_and_english(self):
        self.assertTrue(services._is_korean_or_english("What is the 결정 사항?"))

    def test_rejects_cjk_and_japanese_kana(self):
        self.assertFalse(services._is_korean_or_english("这是中文"))
        self.assertFalse(services._is_korean_or_english("これは日本語です"))

    def test_summarize_rejects_mixed_language_output(self):
        with patch.dict(os.environ, {"OLLAMA_MODEL": "m1"}), patch.object(services, "_generate", return_value="这是总结"):
            self.assertEqual(services.summarize("원문"), "원문")

    def test_chat_reply_rejects_mixed_language_output(self):
        with patch.dict(os.environ, {"OLLAMA_MODEL": "m1"}), patch.object(services, "_generate", return_value="这是回答"):
            self.assertIn("언어 오류", services.chat_reply(self.meeting, "q"))

    def test_retries_once_then_succeeds_on_clean_output(self):
        with patch.dict(os.environ, {"OLLAMA_MODEL": "m1"}), patch.object(
            services, "_generate", side_effect=["这是中文", "정상 요약문"]
        ) as gen:
            result = services.summarize("원문")
        self.assertEqual(result, "정상 요약문")
        self.assertEqual(gen.call_count, 2)

    def test_gives_up_after_one_retry_still_garbled(self):
        with patch.dict(os.environ, {"OLLAMA_MODEL": "m1"}), patch.object(
            services, "_generate", return_value="这是中文"
        ) as gen:
            self.assertEqual(services.summarize("원문"), "원문")
        self.assertEqual(gen.call_count, 2)


@override_settings(BACKGROUND_RECORDING=False)


class LocalSTTTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("a", password="pw12345!")
        self.client.force_login(self.user)
        self.team = Team.objects.create(name="t")
        self.team.members.add(self.user)
        self.meeting = Meeting.objects.create(team=self.team, title="m", host=self.user)

    def test_transcribe_is_skipped_without_model_env(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(services.transcribe("/nonexistent.wav"))

    def test_transcribe_passes_model_and_korean_language(self):
        fake_result = MagicMock(returncode=0, stdout=json.dumps(" 안녕하세요 "), stderr="")
        with patch.dict(os.environ, {"MLX_WHISPER_MODEL": "mlx-community/whisper-large-v3-turbo"}), patch.object(
            services.subprocess, "run", return_value=fake_result
        ) as run:
            self.assertEqual(services.transcribe("/x.wav"), "안녕하세요")
        args = run.call_args[0][0]
        self.assertIn("/x.wav", args)
        self.assertIn("mlx-community/whisper-large-v3-turbo", args)

    def test_transcribe_raises_on_subprocess_failure(self):
        fake_result = MagicMock(returncode=1, stdout="", stderr="ffmpeg decode error")
        with patch.dict(os.environ, {"MLX_WHISPER_MODEL": "mlx-community/whisper-large-v3-turbo"}), patch.object(
            services.subprocess, "run", return_value=fake_result
        ):
            with self.assertRaises(RuntimeError):
                services.transcribe("/x.wav")

    @override_settings(MEDIA_ROOT=tempfile.mkdtemp())
    def test_upload_without_model_leaves_record_empty(self):
        with patch.dict(os.environ, {}, clear=True):
            file = SimpleUploadedFile("a.wav", b"RIFF", content_type="audio/wav")
            self.client.post(reverse("meeting_detail", args=[self.meeting.id]), {"action": "submit", "recording": file})
        self.meeting.refresh_from_db()
        self.assertEqual(self.meeting.record, "")
        self.assertTrue(Recording.objects.filter(meeting=self.meeting).exists())

    @override_settings(MEDIA_ROOT=tempfile.mkdtemp())
    def test_upload_with_model_fills_record_with_transcript(self):
        file = SimpleUploadedFile("a.wav", b"RIFF", content_type="audio/wav")
        with patch.object(services, "transcribe", return_value="전사된 회의 내용"):
            self.client.post(reverse("meeting_detail", args=[self.meeting.id]), {"action": "submit", "recording": file})
        self.meeting.refresh_from_db()
        self.assertEqual(self.meeting.record, "전사된 회의 내용")

    def test_faster_whisper_runs_worker_with_default_device(self):
        fake_result = MagicMock(returncode=0, stdout=json.dumps(" 전사 "), stderr="")
        with patch.dict(os.environ, {"FASTER_WHISPER_MODEL": "large-v3-turbo"}, clear=True), patch.object(
            services.subprocess, "run", return_value=fake_result
        ) as run:
            self.assertEqual(services.transcribe("/x.wav"), "전사")
        args = run.call_args[0][0]
        self.assertEqual(args[0], sys.executable)
        self.assertTrue(args[1].endswith("whisper_worker.py"))
        self.assertEqual(args[2:], ["faster", "/x.wav", "large-v3-turbo", "auto", "default"])

    def test_faster_whisper_device_and_compute_type_come_from_env(self):
        fake_result = MagicMock(returncode=0, stdout=json.dumps("전사"), stderr="")
        env = {"FASTER_WHISPER_MODEL": "turbo", "FASTER_WHISPER_DEVICE": "cpu", "FASTER_WHISPER_COMPUTE_TYPE": "int8"}
        with patch.dict(os.environ, env, clear=True), patch.object(services.subprocess, "run", return_value=fake_result) as run:
            services.transcribe("/x.wav")
        self.assertEqual(run.call_args[0][0][2:], ["faster", "/x.wav", "turbo", "cpu", "int8"])

    def test_mlx_takes_precedence_over_faster_whisper(self):
        fake_result = MagicMock(returncode=0, stdout=json.dumps("전사"), stderr="")
        env = {"MLX_WHISPER_MODEL": "mlx-community/whisper-large-v3-turbo", "FASTER_WHISPER_MODEL": "turbo"}
        with patch.dict(os.environ, env, clear=True), patch.object(services.subprocess, "run", return_value=fake_result) as run:
            services.transcribe("/x.wav")
        self.assertEqual(run.call_args[0][0][2:], ["mlx", "/x.wav", "mlx-community/whisper-large-v3-turbo"])


class ClovaSTTTests(TestCase):
    CLOVA_ENV = {"CLOVA_SPEECH_INVOKE_URL": "https://clovaspeech-gw.ncloud.com/external/v1/1234/abcd", "CLOVA_SPEECH_SECRET_KEY": "secret-1"}
    LOCAL_ENV = {"FASTER_WHISPER_MODEL": "large-v3-turbo"}
    LOCAL_RESULT = MagicMock(returncode=0, stdout=json.dumps("로컬 전사"), stderr="")

    def setUp(self):
        self.user = User.objects.create_user("a", password="pw12345!")
        self.client.force_login(self.user)
        self.team = Team.objects.create(name="t")
        self.team.members.add(self.user)
        self.meeting = Meeting.objects.create(team=self.team, title="m", host=self.user)
        self.audio = os.path.join(tempfile.mkdtemp(), "회의.wav")
        with open(self.audio, "wb") as f:
            f.write(b"RIFF-fake-audio-bytes")

    def _fake_urlopen(self, captured, payload):
        class FakeResponse:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return json.dumps(payload).encode("utf-8")

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["headers"] = dict(request.header_items())
            captured["body"] = b"".join(request.data)
            captured["timeout"] = timeout
            return FakeResponse()
        return fake_urlopen

    def _params(self, body):
        """multipart 본문에서 params 필드의 JSON만 꺼낸다."""
        start = body.index(b"\r\n\r\n", body.index(b'name="params"')) + 4
        return json.loads(body[start:body.index(b"\r\n--", start)].decode("utf-8"))

    def test_clova_needs_both_invoke_url_and_secret_key(self):
        with patch.dict(os.environ, self.CLOVA_ENV, clear=True):
            self.assertTrue(services._clova_enabled())
        for name in self.CLOVA_ENV:
            with patch.dict(os.environ, {name: self.CLOVA_ENV[name]}, clear=True):
                self.assertFalse(services._clova_enabled())

    def test_clova_request_format_and_usage_record(self):
        captured = {}
        payload = {"result": "COMPLETED", "text": " 안녕하세요. 회의 시작합니다. ", "segments": [{"start": 0, "end": 61000, "text": "안녕하세요. 회의 시작합니다."}]}
        with patch.dict(os.environ, self.CLOVA_ENV, clear=True), patch("urllib.request.urlopen", side_effect=self._fake_urlopen(captured, payload)), \
                patch.object(services, "_audio_duration_seconds", return_value=None):
            self.assertEqual(services.transcribe(self.audio, recording_id=None), "안녕하세요. 회의 시작합니다.")
        self.assertEqual(captured["url"], self.CLOVA_ENV["CLOVA_SPEECH_INVOKE_URL"] + "/recognizer/upload")
        self.assertEqual(captured["headers"].get("X-clovaspeech-api-key"), "secret-1")
        self.assertTrue(captured["headers"]["Content-type"].startswith("multipart/form-data; boundary="))
        self.assertEqual(int(captured["headers"]["Content-length"]), len(captured["body"]))
        self.assertEqual(captured["timeout"], services.TRANSCRIBE_TIMEOUT_SECONDS)
        params = self._params(captured["body"])
        self.assertEqual(params["language"], "ko-KR")
        self.assertEqual(params["completion"], "sync")
        self.assertFalse(params["diarization"]["enable"])
        self.assertIn('name="media"; filename="회의.wav"'.encode("utf-8"), captured["body"])
        self.assertIn(b"\r\n\r\nRIFF-fake-audio-bytes\r\n--", captured["body"])
        # 마지막 구간이 61초에서 끝났으니 15초 단위로 올림한 75초가 기록된다.
        self.assertEqual(list(SttUsage.objects.values_list("audio_seconds", flat=True)), [75])

    def test_clova_usage_uses_known_file_length_when_longer_than_speech(self):
        payload = {"result": "COMPLETED", "text": "짧은 말", "segments": [{"start": 0, "end": 5000, "text": "짧은 말"}]}
        with patch.dict(os.environ, self.CLOVA_ENV, clear=True), patch("urllib.request.urlopen", side_effect=self._fake_urlopen({}, payload)), \
                patch.object(services, "_audio_duration_seconds", return_value=100.0):
            services.transcribe(self.audio, recording_id=None)
        self.assertEqual(SttUsage.objects.get().audio_seconds, 105)

    def test_usage_record_points_at_recording_and_survives_its_deletion(self):
        rec = Recording.objects.create(meeting=self.meeting, file=SimpleUploadedFile("a.wav", b"RIFF"))
        payload = {"result": "COMPLETED", "text": "전사", "segments": []}
        with patch.dict(os.environ, self.CLOVA_ENV, clear=True), patch("urllib.request.urlopen", side_effect=self._fake_urlopen({}, payload)), \
                patch.object(services, "_audio_duration_seconds", return_value=None):
            services.transcribe(self.audio, recording_id=rec.id)
        usage = SttUsage.objects.get()
        self.assertEqual(usage.recording_id, rec.id)
        self.assertEqual(usage.audio_seconds, 15)  # 길이를 알 수 없어도 호출 한 번은 최소 과금 단위로 센다
        rec.delete()
        usage.refresh_from_db()
        self.assertIsNone(usage.recording)

    def test_quota_counts_only_this_month(self):
        old = SttUsage.objects.create(audio_seconds=900)
        SttUsage.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=40))
        SttUsage.objects.create(audio_seconds=120)
        self.assertEqual(services.clova_used_seconds(), 120)

    def test_exhausted_quota_switches_to_local_without_calling_clova(self):
        SttUsage.objects.create(audio_seconds=20 * 60)
        with patch.dict(os.environ, {**self.CLOVA_ENV, **self.LOCAL_ENV}, clear=True), patch("urllib.request.urlopen") as urlopen, \
                patch.object(services.subprocess, "run", return_value=self.LOCAL_RESULT) as run:
            self.assertEqual(services.transcribe(self.audio), "로컬 전사")
        urlopen.assert_not_called()
        self.assertEqual(run.call_args[0][0][2], "faster")
        self.assertEqual(SttUsage.objects.count(), 1)

    def test_file_that_would_exceed_remaining_quota_goes_local(self):
        SttUsage.objects.create(audio_seconds=15 * 60)
        with patch.dict(os.environ, {**self.CLOVA_ENV, **self.LOCAL_ENV}, clear=True), patch("urllib.request.urlopen") as urlopen, \
                patch.object(services.subprocess, "run", return_value=self.LOCAL_RESULT), \
                patch.object(services, "_audio_duration_seconds", return_value=400.0):
            self.assertEqual(services.transcribe(self.audio), "로컬 전사")  # 15분 + 6분 45초 > 20분
        urlopen.assert_not_called()

    def test_file_that_fits_remaining_quota_uses_clova(self):
        SttUsage.objects.create(audio_seconds=15 * 60)
        payload = {"result": "COMPLETED", "text": "클로바 전사", "segments": []}
        with patch.dict(os.environ, {**self.CLOVA_ENV, **self.LOCAL_ENV}, clear=True), \
                patch("urllib.request.urlopen", side_effect=self._fake_urlopen({}, payload)), \
                patch.object(services.subprocess, "run") as run, patch.object(services, "_audio_duration_seconds", return_value=290.0):
            self.assertEqual(services.transcribe(self.audio), "클로바 전사")  # 15분 + 5분 = 딱 20분
        run.assert_not_called()
        self.assertEqual(services.clova_used_seconds(), 20 * 60)

    def test_free_minutes_env_overrides_default_quota(self):
        SttUsage.objects.create(audio_seconds=30 * 60)
        payload = {"result": "COMPLETED", "text": "클로바 전사", "segments": []}
        with patch.dict(os.environ, {**self.CLOVA_ENV, "CLOVA_SPEECH_FREE_MINUTES": "60"}, clear=True), \
                patch("urllib.request.urlopen", side_effect=self._fake_urlopen({}, payload)), \
                patch.object(services, "_audio_duration_seconds", return_value=None):
            self.assertEqual(services.transcribe(self.audio), "클로바 전사")

    def test_exhausted_quota_without_local_model_fails_with_reason(self):
        SttUsage.objects.create(audio_seconds=20 * 60)
        with patch.dict(os.environ, self.CLOVA_ENV, clear=True), patch("urllib.request.urlopen") as urlopen:
            with self.assertRaises(RuntimeError) as ctx:
                services.transcribe(self.audio)
        self.assertIn("무료 사용량(20분)을 다 썼습니다", str(ctx.exception))
        urlopen.assert_not_called()

    def test_clova_http_error_falls_back_to_local_and_records_nothing(self):
        import urllib.error
        error = urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)
        with patch.dict(os.environ, {**self.CLOVA_ENV, **self.LOCAL_ENV}, clear=True), patch("urllib.request.urlopen", side_effect=error), \
                patch.object(services.subprocess, "run", return_value=self.LOCAL_RESULT) as run, \
                patch.object(services, "_audio_duration_seconds", return_value=None), self.assertLogs(services.logger, "WARNING") as logs:
            self.assertEqual(services.transcribe(self.audio), "로컬 전사")
        self.assertEqual(run.call_args[0][0][2], "faster")
        self.assertEqual(SttUsage.objects.count(), 0)
        self.assertIn("CLOVA Speech 오류 401", logs.output[0])

    def test_clova_http_error_without_local_model_shows_server_message(self):
        import urllib.error
        error = urllib.error.HTTPError("u", 401, "Unauthorized", {}, io.BytesIO(b'{"message": "Invalid secret"}'))
        with patch.dict(os.environ, self.CLOVA_ENV, clear=True), patch("urllib.request.urlopen", side_effect=error), \
                patch.object(services, "_audio_duration_seconds", return_value=None):
            with self.assertRaises(RuntimeError) as ctx:
                services.transcribe(self.audio)
        self.assertIn("CLOVA Speech 오류 401", str(ctx.exception))
        self.assertIn("Invalid secret", str(ctx.exception))

    def test_clova_connection_error_and_bad_result_are_reported(self):
        with patch.dict(os.environ, self.CLOVA_ENV, clear=True), patch.object(services, "_audio_duration_seconds", return_value=None):
            with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
                with self.assertRaises(RuntimeError) as ctx:
                    services.transcribe(self.audio)
            self.assertIn("연결 실패", str(ctx.exception))
            failed = {"result": "FAILED", "message": "ERROR_AUDIO_CONVERSION"}
            with patch("urllib.request.urlopen", side_effect=self._fake_urlopen({}, failed)):
                with self.assertRaises(RuntimeError) as ctx:
                    services.transcribe(self.audio)
            self.assertIn("ERROR_AUDIO_CONVERSION", str(ctx.exception))
        self.assertEqual(SttUsage.objects.count(), 0)

    def test_diarization_groups_consecutive_speaker_turns(self):
        captured = {}
        payload = {
            "result": "COMPLETED", "text": "안녕하세요. 반갑습니다. 네 저도요.",
            "segments": [
                {"end": 1000, "text": "안녕하세요.", "speaker": {"label": "1", "name": "A"}},
                {"end": 2000, "text": "반갑습니다.", "speaker": {"label": "1", "name": "A"}},
                {"end": 3000, "text": "네 저도요.", "speaker": {"label": "2", "name": "B"}},
            ],
        }
        with patch.dict(os.environ, {**self.CLOVA_ENV, "CLOVA_SPEECH_DIARIZATION": "1"}, clear=True), \
                patch("urllib.request.urlopen", side_effect=self._fake_urlopen(captured, payload)), \
                patch.object(services, "_audio_duration_seconds", return_value=None):
            self.assertEqual(services.transcribe(self.audio), "화자 A: 안녕하세요. 반갑습니다.\n화자 B: 네 저도요.")
        self.assertTrue(self._params(captured["body"])["diarization"]["enable"])

    def test_diarization_with_single_speaker_keeps_plain_text(self):
        payload = {"result": "COMPLETED", "text": "혼자 말함.", "segments": [{"end": 1000, "text": "혼자 말함.", "speaker": {"label": "1", "name": "A"}}]}
        with patch.dict(os.environ, {**self.CLOVA_ENV, "CLOVA_SPEECH_DIARIZATION": "1"}, clear=True), \
                patch("urllib.request.urlopen", side_effect=self._fake_urlopen({}, payload)), \
                patch.object(services, "_audio_duration_seconds", return_value=None):
            self.assertEqual(services.transcribe(self.audio), "혼자 말함.")

    def test_stt_status_text_variants(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(services.stt_status_text())
        with patch.dict(os.environ, self.LOCAL_ENV, clear=True):
            self.assertEqual(services.stt_status_text(), "음성 인식: 로컬 모델(faster-whisper)")
        SttUsage.objects.create(audio_seconds=750)
        with patch.dict(os.environ, {**self.CLOVA_ENV, **self.LOCAL_ENV}, clear=True):
            self.assertEqual(
                services.stt_status_text(),
                "음성 인식: CLOVA Speech · 이번 달 무료 사용량 12.5/20분 (소진 시 로컬 모델(faster-whisper)로 전환)",
            )
        with patch.dict(os.environ, {**self.CLOVA_ENV, "CLOVA_SPEECH_FREE_MINUTES": "60"}, clear=True):
            self.assertEqual(services.stt_status_text(), "음성 인식: CLOVA Speech · 이번 달 무료 사용량 12.5/60분")
        SttUsage.objects.create(audio_seconds=450)
        with patch.dict(os.environ, self.CLOVA_ENV, clear=True):
            self.assertIn("20/20분 (소진 — 내 컴퓨터용 음성 인식 모델이 없어 글로 옮기지 못합니다)", services.stt_status_text())
        with patch.dict(os.environ, {**self.CLOVA_ENV, **self.LOCAL_ENV}, clear=True):
            self.assertIn("(소진 → 로컬 모델(faster-whisper)로 전환)", services.stt_status_text())

    def test_meeting_detail_shows_stt_status_only_when_configured(self):
        url = reverse("meeting_detail", args=[self.meeting.id])
        with patch.dict(os.environ, {}, clear=True):
            self.assertNotContains(self.client.get(url), "전사 엔진")
        with patch.dict(os.environ, self.CLOVA_ENV, clear=True):
            self.assertContains(self.client.get(url), "음성 인식: CLOVA Speech · 이번 달 무료 사용량 0/20분")


class GroqSTTTests(TestCase):
    CLOVA_ENV = ClovaSTTTests.CLOVA_ENV
    GROQ_ENV = {"GROQ_API_KEY": "groq-key-1"}
    LOCAL_ENV = {"FASTER_WHISPER_MODEL": "large-v3-turbo"}
    LOCAL_RESULT = MagicMock(returncode=0, stdout=json.dumps("로컬 전사"), stderr="")

    def setUp(self):
        self.audio = os.path.join(tempfile.mkdtemp(), "회의.wav")
        with open(self.audio, "wb") as f:
            f.write(b"RIFF-fake-audio-bytes")

    def _fake_urlopen(self, captured, text):
        class FakeResponse:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return text.encode("utf-8")

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["headers"] = dict(request.header_items())
            captured["body"] = b"".join(request.data)
            captured["timeout"] = timeout
            return FakeResponse()
        return fake_urlopen

    def test_groq_enabled_requires_api_key(self):
        with patch.dict(os.environ, self.GROQ_ENV, clear=True):
            self.assertTrue(services._groq_enabled())
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(services._groq_enabled())

    def test_groq_request_format(self):
        captured = {}
        with patch.dict(os.environ, self.GROQ_ENV, clear=True), \
                patch("urllib.request.urlopen", side_effect=self._fake_urlopen(captured, " 안녕하세요, 회의 시작합니다. ")):
            self.assertEqual(services.transcribe(self.audio), "안녕하세요, 회의 시작합니다.")
        self.assertEqual(captured["url"], services.GROQ_URL)
        self.assertEqual(captured["headers"].get("Authorization"), "Bearer groq-key-1")
        self.assertEqual(captured["headers"].get("User-agent"), services.GROQ_USER_AGENT)
        self.assertTrue(captured["headers"]["Content-type"].startswith("multipart/form-data; boundary="))
        self.assertEqual(int(captured["headers"]["Content-length"]), len(captured["body"]))
        self.assertEqual(captured["timeout"], services.TRANSCRIBE_TIMEOUT_SECONDS)
        self.assertIn(b'name="model"\r\n\r\nwhisper-large-v3-turbo\r\n', captured["body"])
        self.assertIn(b'name="language"\r\n\r\nko\r\n', captured["body"])
        self.assertIn(b'name="response_format"\r\n\r\ntext\r\n', captured["body"])
        self.assertIn('name="file"; filename="회의.wav"'.encode("utf-8"), captured["body"])
        self.assertIn(b"\r\n\r\nRIFF-fake-audio-bytes\r\n--", captured["body"])

    def test_groq_model_env_overrides_default(self):
        captured = {}
        with patch.dict(os.environ, {**self.GROQ_ENV, "GROQ_MODEL": "whisper-large-v3"}, clear=True), \
                patch("urllib.request.urlopen", side_effect=self._fake_urlopen(captured, "전사")):
            services.transcribe(self.audio)
        self.assertIn(b'name="model"\r\n\r\nwhisper-large-v3\r\n', captured["body"])

    def test_groq_used_when_clova_quota_exhausted(self):
        SttUsage.objects.create(audio_seconds=20 * 60)
        with patch.dict(os.environ, {**self.CLOVA_ENV, **self.GROQ_ENV}, clear=True), \
                patch("urllib.request.urlopen", side_effect=self._fake_urlopen({}, "그록 전사")):
            self.assertEqual(services.transcribe(self.audio), "그록 전사")
        self.assertEqual(SttUsage.objects.count(), 1)  # CLOVA 호출이 없었으니 그대로

    def test_groq_used_after_clova_http_error(self):
        import urllib.error
        error = urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)
        calls = {"n": 0}

        def side_effect(request, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                raise error
            return self._fake_urlopen({}, "그록 전사")(request, timeout)

        with patch.dict(os.environ, {**self.CLOVA_ENV, **self.GROQ_ENV}, clear=True), \
                patch("urllib.request.urlopen", side_effect=side_effect), \
                patch.object(services, "_audio_duration_seconds", return_value=None), \
                self.assertLogs(services.logger, "WARNING"):
            self.assertEqual(services.transcribe(self.audio), "그록 전사")
        self.assertEqual(calls["n"], 2)

    def test_groq_failure_falls_back_to_local(self):
        import urllib.error
        error = urllib.error.HTTPError("u", 500, "Server Error", {}, None)
        with patch.dict(os.environ, {**self.GROQ_ENV, **self.LOCAL_ENV}, clear=True), \
                patch("urllib.request.urlopen", side_effect=error), \
                patch.object(services.subprocess, "run", return_value=self.LOCAL_RESULT) as run, \
                self.assertLogs(services.logger, "WARNING") as logs:
            self.assertEqual(services.transcribe(self.audio), "로컬 전사")
        self.assertEqual(run.call_args[0][0][2], "faster")
        self.assertIn("Groq Whisper 오류 500", logs.output[0])

    def test_groq_failure_without_local_raises(self):
        import urllib.error
        error = urllib.error.HTTPError("u", 500, "Server Error", {}, io.BytesIO(b"rate limited"))
        with patch.dict(os.environ, self.GROQ_ENV, clear=True), patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(RuntimeError) as ctx:
                services.transcribe(self.audio)
        self.assertIn("Groq Whisper 오류 500", str(ctx.exception))
        self.assertIn("rate limited", str(ctx.exception))

    def test_stt_status_text_prefers_groq_over_local_as_fallback_label(self):
        with patch.dict(os.environ, {**self.CLOVA_ENV, **self.GROQ_ENV, **self.LOCAL_ENV}, clear=True):
            self.assertIn("(소진 시 Groq Whisper로 전환)", services.stt_status_text())
        with patch.dict(os.environ, self.GROQ_ENV, clear=True):
            self.assertEqual(services.stt_status_text(), "음성 인식: Groq Whisper")


class LocalDiarizationTests(TestCase):
    """LOCAL_DIARIZATION=1일 때 Groq·로컬 Whisper 구간에 로컬 화자 분리 결과를 붙이는 흐름. 실제 모델은 돌리지 않는다."""

    GROQ_ENV = {"GROQ_API_KEY": "groq-key-1", "LOCAL_DIARIZATION": "1"}
    SEGMENTS = [
        {"start": 0.0, "end": 2.0, "text": "안녕하세요, 시작할게요."},
        {"start": 2.5, "end": 4.0, "text": "네, 잘 들려요."},
        {"start": 4.2, "end": 6.0, "text": "저는 백엔드를 맡을게요."},
        {"start": 6.5, "end": 8.0, "text": "그럼 다음 주에 봬요."},
    ]
    TURNS = [[0.0, 2.1, 3], [2.4, 6.1, 1], [6.4, 8.0, 3]]

    def setUp(self):
        self.audio = os.path.join(tempfile.mkdtemp(), "회의.wav")
        with open(self.audio, "wb") as f:
            f.write(b"RIFF-fake-audio-bytes")

    def test_segments_are_grouped_into_speaker_lines_in_order_of_first_appearance(self):
        text = services._label_speakers(self.SEGMENTS, self.TURNS)
        self.assertEqual(text, "화자 A: 안녕하세요, 시작할게요.\n화자 B: 네, 잘 들려요. 저는 백엔드를 맡을게요.\n화자 A: 그럼 다음 주에 봬요.")

    def test_a_single_speaker_gets_no_labels(self):
        self.assertEqual(services._label_speakers(self.SEGMENTS[:2], [[0.0, 8.0, 0]]), "안녕하세요, 시작할게요. 네, 잘 들려요.")

    def test_segment_without_overlap_goes_to_the_nearest_speaker(self):
        self.assertEqual(services._speaker_for({"start": 9.0, "end": 9.5, "text": "네"}, self.TURNS), 3)

    def test_groq_is_asked_for_segments_and_labeled(self):
        with patch.dict(os.environ, self.GROQ_ENV, clear=True), \
                patch.object(services, "_transcribe_groq", return_value=self.SEGMENTS) as groq, \
                patch.object(services, "_diarize", return_value=self.TURNS) as diarize:
            text = services.transcribe(self.audio)
        groq.assert_called_once_with(self.audio, with_segments=True)
        diarize.assert_called_once_with(self.audio)
        self.assertTrue(text.startswith("화자 A: 안녕하세요"))

    def test_diarization_failure_keeps_the_transcript_without_labels(self):
        with patch.dict(os.environ, self.GROQ_ENV, clear=True), \
                patch.object(services, "_transcribe_groq", return_value=self.SEGMENTS), \
                patch.object(services, "_diarize", side_effect=RuntimeError("sherpa_onnx 없음")), \
                self.assertLogs(services.logger, "WARNING"):
            text = services.transcribe(self.audio)
        self.assertEqual(text, " ".join(segment["text"] for segment in self.SEGMENTS))

    def test_isolated_word_between_the_same_speaker_is_smoothed(self):
        words = [{"start": float(i), "end": i + 0.9, "text": text} for i, text in enumerate(["그럼", "다음", "주에", "봬요."])]
        # 1~2초 구간만 다른 화자로 잡혀도 앞뒤가 같은 화자면 한 줄로 이어진다.
        turns = [[0.0, 1.0, 0], [1.0, 2.0, 1], [2.0, 4.0, 0], [4.0, 6.0, 1]]
        self.assertEqual(services._label_speakers(words, turns), "그럼 다음 주에 봬요.")

    def test_groq_word_timestamps_are_preferred_over_segments(self):
        captured = {}
        payload = json.dumps({
            "text": "전체", "segments": [{"start": 0, "end": 2, "text": " 안녕하세요. 네."}],
            "words": [{"word": "안녕하세요.", "start": 0, "end": 1.2}, {"word": "네.", "start": 1.4, "end": 2}],
        })

        class FakeResponse:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return payload.encode("utf-8")

        def fake_urlopen(request, timeout):
            captured["body"] = b"".join(request.data)
            return FakeResponse()

        with patch.dict(os.environ, self.GROQ_ENV, clear=True), patch("urllib.request.urlopen", side_effect=fake_urlopen):
            words = services._transcribe_groq(self.audio, with_segments=True)
        self.assertEqual([w["text"] for w in words], ["안녕하세요.", "네."])
        self.assertIn(b'name="timestamp_granularities[]"\r\n\r\nword\r\n', captured["body"])
        self.assertIn(b'name="timestamp_granularities[]"\r\n\r\nsegment\r\n', captured["body"])

    def test_groq_verbose_json_segments_are_parsed(self):
        captured = {}
        payload = json.dumps({"text": "전체", "segments": [{"start": 0, "end": 1.5, "text": " 안녕하세요 "}, {"start": 1.5, "end": 2, "text": " "}]})

        class FakeResponse:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return payload.encode("utf-8")

        def fake_urlopen(request, timeout):
            captured["body"] = b"".join(request.data)
            return FakeResponse()

        with patch.dict(os.environ, self.GROQ_ENV, clear=True), patch("urllib.request.urlopen", side_effect=fake_urlopen):
            segments = services._transcribe_groq(self.audio, with_segments=True)
        self.assertEqual(segments, [{"start": 0.0, "end": 1.5, "text": "안녕하세요"}])
        self.assertIn(b'name="response_format"\r\n\r\nverbose_json\r\n', captured["body"])

    def test_local_whisper_is_asked_for_segments(self):
        result = MagicMock(returncode=0, stdout=json.dumps(self.SEGMENTS), stderr="")
        env = {"FASTER_WHISPER_MODEL": "large-v3-turbo", "LOCAL_DIARIZATION": "1"}
        with patch.dict(os.environ, env, clear=True), \
                patch.object(services.subprocess, "run", return_value=result) as run, \
                patch.object(services, "_diarize", return_value=self.TURNS):
            text = services.transcribe(self.audio)
        self.assertEqual(run.call_args[0][0][-1], "--segments")
        self.assertIn("화자 B: 네, 잘 들려요.", text)

    def test_stt_status_mentions_local_diarization(self):
        with patch.dict(os.environ, self.GROQ_ENV, clear=True):
            self.assertEqual(services.stt_status_text(), "음성 인식: Groq Whisper · 말한 사람 구분(내 컴퓨터)")


class WhisperWorkerTests(TestCase):
    def _fake_faster_whisper(self, segment_texts, calls):
        class FakeSegment:
            def __init__(self, text):
                self.text = text

        class FakeModel:
            def __init__(self, model, device, compute_type):
                calls["init"] = (model, device, compute_type)

            def transcribe(self, path, **kwargs):
                calls["transcribe"] = (path, kwargs)
                return (FakeSegment(text) for text in segment_texts), object()

        module = types.ModuleType("faster_whisper")
        module.WhisperModel = FakeModel
        return module

    def test_faster_whisper_joins_segments_and_passes_options(self):
        calls = {}
        module = self._fake_faster_whisper([" 안녕하세요 ", "   ", " 회의를 시작합니다 "], calls)
        with patch.dict(sys.modules, {"faster_whisper": module}), patch.object(whisper_worker, "_add_windows_cuda_dlls") as dlls:
            text = whisper_worker.transcribe_faster("/x.wav", "large-v3-turbo", "cpu", "int8")
        self.assertEqual(text, "안녕하세요 회의를 시작합니다")
        self.assertEqual(calls["init"], ("large-v3-turbo", "cpu", "int8"))
        self.assertEqual(calls["transcribe"][0], "/x.wav")
        self.assertEqual(calls["transcribe"][1]["language"], "ko")
        self.assertTrue(calls["transcribe"][1]["vad_filter"])
        dlls.assert_called_once()

    def test_main_dispatches_engine_and_prints_json(self):
        with patch.object(whisper_worker, "transcribe_faster", return_value="전사 결과") as faster, patch("sys.stdout", new_callable=io.StringIO) as out:
            whisper_worker.main(["whisper_worker.py", "faster", "/x.wav", "turbo", "cuda", "float16"])
        faster.assert_called_once_with("/x.wav", "turbo", "cuda", "float16")
        self.assertEqual(json.loads(out.getvalue()), "전사 결과")
        with patch.object(whisper_worker, "transcribe_mlx", return_value="맥 전사") as mlx, patch("sys.stdout", new_callable=io.StringIO) as out:
            whisper_worker.main(["whisper_worker.py", "mlx", "/x.wav", "mlx-community/whisper-large-v3-turbo"])
        mlx.assert_called_once_with("/x.wav", "mlx-community/whisper-large-v3-turbo")
        self.assertEqual(json.loads(out.getvalue()), "맥 전사")

    def test_segments_flag_returns_timed_segments(self):
        class FakeModel:
            def __init__(self, model, device, compute_type):
                pass

            def transcribe(self, path, **kwargs):
                assert kwargs["word_timestamps"]
                word = lambda start, text: types.SimpleNamespace(start=start, end=start + 0.4, word=text)
                return iter([
                    types.SimpleNamespace(words=[word(0.0, " 안녕하세요."), word(0.5, " ")]),
                    types.SimpleNamespace(words=[word(1.0, " 회의를"), word(1.5, " 시작합니다.")]),
                ]), object()

        module = types.ModuleType("faster_whisper")
        module.WhisperModel = FakeModel
        with patch.dict(sys.modules, {"faster_whisper": module}), patch.object(whisper_worker, "_add_windows_cuda_dlls"), \
                patch("sys.stdout", new_callable=io.StringIO) as out:
            whisper_worker.main(["whisper_worker.py", "faster", "/x.wav", "turbo", "cpu", "int8", "--segments"])
        self.assertEqual(json.loads(out.getvalue()), [
            {"start": 0.0, "end": 0.4, "text": "안녕하세요."},
            {"start": 1.0, "end": 1.4, "text": "회의를"},
            {"start": 1.5, "end": 1.9, "text": "시작합니다."},
        ])

    def test_windows_adds_pip_installed_nvidia_dll_dirs(self):
        root = tempfile.mkdtemp()
        for name in ("cublas", "cudnn"):
            os.makedirs(os.path.join(root, "nvidia", name, "bin"))
        os.makedirs(os.path.join(root, "nvidia", "no_bin_here"))
        added = []
        with patch.object(whisper_worker.sys, "platform", "win32"), \
                patch.object(whisper_worker.sysconfig, "get_paths", return_value={"purelib": root}), \
                patch.object(whisper_worker.os, "add_dll_directory", create=True, side_effect=added.append), \
                patch.dict(os.environ, {"PATH": "ORIGINAL"}):
            whisper_worker._add_windows_cuda_dlls()
            path = os.environ["PATH"]
        cublas, cudnn = (os.path.join(root, "nvidia", name, "bin") for name in ("cublas", "cudnn"))
        self.assertEqual(added, [cublas, cudnn])
        self.assertEqual(path, os.pathsep.join([cudnn, cublas, "ORIGINAL"]))

    def test_cuda_dll_setup_does_nothing_outside_windows_or_without_packages(self):
        with patch.object(whisper_worker.sys, "platform", "darwin"), patch.object(whisper_worker.sysconfig, "get_paths") as paths:
            whisper_worker._add_windows_cuda_dlls()
        paths.assert_not_called()
        with patch.object(whisper_worker.sys, "platform", "win32"), \
                patch.object(whisper_worker.sysconfig, "get_paths", return_value={"purelib": tempfile.mkdtemp()}), \
                patch.object(whisper_worker.os, "add_dll_directory", create=True) as add:
            whisper_worker._add_windows_cuda_dlls()
        add.assert_not_called()


class SecurityFixTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("a", password="pw12345!")
        self.client.force_login(self.user)
        self.team = Team.objects.create(name="t")
        self.team.members.add(self.user)
        self.meeting = Meeting.objects.create(team=self.team, title="m", host=self.user)

    def _login_with_next(self, next_url):
        self.client.logout()
        return self.client.post(reverse("login") + "?next=" + next_url, {"username": "a", "password": "pw12345!"})

    def test_login_ignores_external_next_url(self):
        self.assertRedirects(self._login_with_next("https://evil.example/"), reverse("index"), fetch_redirect_response=False)

    def test_login_follows_internal_next_url(self):
        target = reverse("meeting_list", args=[self.team.id])
        self.assertRedirects(self._login_with_next(target), target, fetch_redirect_response=False)

    def test_invalid_deadline_is_rejected_with_message(self):
        for raw in ["not-a-date", "2030-13-01"]:
            r = self.client.post(reverse("meeting_create", args=[self.team.id]), {"title": "bad", "deadline": raw})
            self.assertEqual(r.status_code, 200)
            self.assertContains(r, "마감일 형식이 올바르지 않습니다.")
        self.assertFalse(Meeting.objects.filter(title="bad").exists())

    def test_past_deadline_is_rejected_with_message(self):
        past = (timezone.localdate() - timedelta(days=1)).isoformat()
        r = self.client.post(reverse("meeting_create", args=[self.team.id]), {"title": "bad", "deadline": past})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "마감일은 오늘 이후 날짜로 설정해주세요.")
        self.assertFalse(Meeting.objects.filter(title="bad").exists())

    def test_todays_date_is_accepted_as_deadline(self):
        today = timezone.localdate().isoformat()
        self.client.post(reverse("meeting_create", args=[self.team.id]), {"title": "due-today", "deadline": today})
        self.assertEqual(str(Meeting.objects.get(title="due-today").deadline), today)

    @override_settings(MEDIA_ROOT=tempfile.mkdtemp())
    def test_unsupported_recording_format_is_rejected(self):
        file = SimpleUploadedFile("evil.exe", b"MZ", content_type="application/octet-stream")
        r = self.client.post(reverse("meeting_detail", args=[self.meeting.id]), {"action": "submit", "recording": file})
        self.assertContains(r, "지원하지 않는 녹음 형식")
        self.meeting.refresh_from_db()
        self.assertFalse(Recording.objects.filter(meeting=self.meeting).exists())

    @override_settings(MEDIA_ROOT=tempfile.mkdtemp(), BACKGROUND_RECORDING=False)
    def test_additional_audio_video_formats_are_accepted(self):
        for name in ["a.aac", "a.wma", "a.opus", "a.amr", "a.caf", "a.avi", "a.mov", "a.mkv", "a.3gp"]:
            file = SimpleUploadedFile(name, b"data", content_type="application/octet-stream")
            r = self.client.post(reverse("meeting_detail", args=[self.meeting.id]), {"action": "submit", "recording": file})
            self.assertEqual(r.status_code, 302, name)

    @override_settings(MEDIA_ROOT=tempfile.mkdtemp())
    def test_oversized_recording_is_rejected(self):
        file = SimpleUploadedFile("a.wav", b"RIFF", content_type="audio/wav")
        with patch.object(views, "MAX_RECORDING_BYTES", 2):
            r = self.client.post(reverse("meeting_detail", args=[self.meeting.id]), {"action": "submit", "recording": file})
        self.assertContains(r, "너무 큽니다")
        self.meeting.refresh_from_db()
        self.assertFalse(Recording.objects.filter(meeting=self.meeting).exists())

    def test_anonymous_question_blocked_after_meeting_ends(self):
        self.meeting.ended = True
        self.meeting.save()
        r = self.client.post(reverse("chat", args=[self.meeting.id]), {"mode": "anon", "text": "late"})
        self.assertContains(r, "종료된 회의에는 익명 질문을 보낼 수 없습니다.")
        self.assertFalse(Question.objects.filter(meeting=self.meeting).exists())

    @override_settings(MEDIA_ROOT=tempfile.mkdtemp())
    def test_recording_blocked_after_meeting_ends(self):
        self.meeting.ended = True
        self.meeting.save()
        file = SimpleUploadedFile("a.wav", b"RIFF", content_type="audio/wav")
        r = self.client.post(reverse("meeting_detail", args=[self.meeting.id]), {"action": "submit", "recording": file})
        self.assertContains(r, "종료된 회의에는 녹음을 올릴 수 없습니다.")
        self.meeting.refresh_from_db()
        self.assertFalse(Recording.objects.filter(meeting=self.meeting).exists())


@override_settings(BACKGROUND_RECORDING=False)


class MeetingSummaryTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("a", password="pw12345!")
        self.client.force_login(self.user)
        self.team = Team.objects.create(name="t")
        self.team.members.add(self.user)
        self.meeting = Meeting.objects.create(team=self.team, title="m", host=self.user)

    def test_summarize_returns_transcript_when_llm_disabled(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(services.summarize("원문"), "원문")

    def test_summarize_uses_llm_output(self):
        with patch.dict(os.environ, {"OLLAMA_MODEL": "m1"}), patch.object(services, "_generate", return_value="[결정 사항]\n- A안") as gen:
            self.assertEqual(services.summarize("원문"), "[결정 사항]\n- A안")
        self.assertIn("원문", gen.call_args[0][0])

    def test_summarize_falls_back_to_transcript_on_failure(self):
        with patch.dict(os.environ, {"OLLAMA_MODEL": "m1"}), patch.object(services, "_generate", side_effect=OSError("down")):
            self.assertEqual(services.summarize("원문"), "원문")

    @override_settings(MEDIA_ROOT=tempfile.mkdtemp())
    def test_upload_stores_transcript_and_summary(self):
        file = SimpleUploadedFile("a.wav", b"RIFF", content_type="audio/wav")
        with patch.object(services, "transcribe", return_value="원문 전사"), \
             patch.object(services, "summarize", return_value="요약본"):
            self.client.post(reverse("meeting_detail", args=[self.meeting.id]), {"action": "submit", "recording": file})
        self.meeting.refresh_from_db()
        self.assertEqual(self.meeting.transcript, "원문 전사")
        self.assertEqual(self.meeting.record, "요약본")

    def test_summary_page_shows_transcript_details(self):
        self.meeting.transcript = "녹취 원문 내용"
        self.meeting.save()
        r = self.client.get(reverse("summary", args=[self.meeting.id]))
        self.assertContains(r, "녹취 원문 보기")
        self.assertContains(r, "녹취 원문 내용")


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(), BACKGROUND_RECORDING=False)


class BackgroundProcessingTests(TestCase):
    def setUp(self):
        # Gemini 실패 후 건너뛰기 상태는 모듈 전역이라, 앞 테스트에서 걸린 것이 다음 테스트로 넘어가지 않게 매번 푼다.
        services._gemini_skip_until.clear()
        self.user = User.objects.create_user("a", password="pw12345!")
        self.client.force_login(self.user)
        self.team = Team.objects.create(name="t")
        self.team.members.add(self.user)
        self.meeting = Meeting.objects.create(team=self.team, title="m", host=self.user)

    def _upload(self):
        file = SimpleUploadedFile("a.wav", b"RIFF", content_type="audio/wav")
        return self.client.post(reverse("meeting_detail", args=[self.meeting.id]), {"action": "submit", "recording": file})

    def test_upload_marks_processing_then_done(self):
        with patch.object(services, "transcribe", return_value="전사"), patch.object(services, "summarize", return_value="요약"):
            self._upload()
        self.meeting.refresh_from_db()
        self.assertEqual(self.meeting.processing_status, Meeting.STATUS_DONE)
        self.assertEqual(self.meeting.record, "요약")

    def test_failure_marks_failed(self):
        with patch.object(services, "transcribe", side_effect=RuntimeError("boom")):
            self._upload()
        self.meeting.refresh_from_db()
        self.assertEqual(self.meeting.processing_status, Meeting.STATUS_FAILED)

    def test_failure_records_error_message_for_display(self):
        with patch.object(services, "transcribe", side_effect=RuntimeError("ffmpeg not found")):
            self._upload()
        rec = self.meeting.recordings.get()
        self.assertEqual(rec.error_message, "ffmpeg not found")
        r = self.client.get(reverse("meeting_detail", args=[self.meeting.id]))
        self.assertContains(r, "ffmpeg not found")

    def test_retry_clears_previous_error_message(self):
        rec = Recording.objects.create(
            meeting=self.meeting, file=SimpleUploadedFile("a.wav", b"RIFF"),
            status=Recording.STATUS_FAILED, error_message="이전 오류",
        )
        with patch.object(tasks, "start_processing"):
            self.client.post(reverse("recording_retry", args=[self.meeting.id, rec.id]))
        rec.refresh_from_db()
        self.assertEqual(rec.error_message, "")

    def test_result_page_shows_processing_message_and_refresh(self):
        self.meeting.processing_status = Meeting.STATUS_PROCESSING
        self.meeting.save()
        tasks._active.add(self.meeting.id)
        self.addCleanup(tasks._active.discard, self.meeting.id)
        r = self.client.get(reverse("meeting_result", args=[self.meeting.id]))
        self.assertContains(r, "http-equiv=\"refresh\"")
        self.assertContains(r, "녹음을 글로 옮기고")

    def test_background_mode_starts_a_thread_instead_of_running_inline(self):
        rec = Recording.objects.create(meeting=self.meeting, file=SimpleUploadedFile("a.wav", b"RIFF"), status=Recording.STATUS_PROCESSING)
        with override_settings(BACKGROUND_RECORDING=True), patch.object(tasks.threading, "Thread") as thread, \
             patch.object(tasks, "process_recording") as inline:
            tasks.start_processing(rec.id)
        thread.assert_called_once()
        thread.return_value.start.assert_called_once()
        inline.assert_not_called()

    def test_ollama_request_sets_context_window(self):
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return b'{"response": "ok"}'

        def fake_urlopen(request, timeout):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse()

        with patch.dict(os.environ, {"OLLAMA_MODEL": "m1", "GEMINI_API_KEY": ""}), patch("urllib.request.urlopen", side_effect=fake_urlopen):
            services._generate("hi")
        self.assertEqual(captured["body"]["options"]["num_ctx"], 16384)

    def _fake_gemini_urlopen(self, captured, payload):
        class FakeResponse:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return json.dumps(payload).encode("utf-8")

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["headers"] = dict(request.header_items())
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse()
        return fake_urlopen

    def test_gemini_needs_both_api_key_and_model(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "k1", "GEMINI_MODEL": "gemini-x"}, clear=True):
            self.assertTrue(services._enabled())
        with patch.dict(os.environ, {"GEMINI_API_KEY": "k1"}, clear=True):
            self.assertFalse(services._enabled())
        with patch.dict(os.environ, {"GEMINI_MODEL": "gemini-x"}, clear=True):
            self.assertFalse(services._enabled())

    def test_gemini_request_and_response(self):
        captured = {}
        payload = {"candidates": [{"content": {"parts": [{"text": " 답변 "}]}}]}
        fake = self._fake_gemini_urlopen(captured, payload)
        with patch.dict(os.environ, {"GEMINI_API_KEY": "k1", "GEMINI_MODEL": "gemini-x", "OLLAMA_MODEL": "m1"}, clear=True), patch("urllib.request.urlopen", side_effect=fake):
            result = services._generate("hi", temperature=0.3)
        self.assertEqual(result, "답변")
        self.assertIn("/models/gemini-x:generateContent", captured["url"])
        self.assertEqual(captured["headers"].get("X-goog-api-key"), "k1")
        self.assertEqual(captured["body"]["contents"][0]["parts"][0]["text"], "hi")
        self.assertEqual(captured["body"]["generationConfig"]["temperature"], 0.3)

    def test_gemini_empty_response_falls_back(self):
        fake = self._fake_gemini_urlopen({}, {"promptFeedback": {"blockReason": "SAFETY"}})
        with patch.dict(os.environ, {"GEMINI_API_KEY": "k1", "GEMINI_MODEL": "gemini-x"}, clear=True), patch("urllib.request.urlopen", side_effect=fake):
            self.assertRaises(OSError, services._generate, "hi")
            self.assertEqual(services.summarize("전사본"), "전사본")

    def test_gemini_http_error_falls_back(self):
        import urllib.error
        error = urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None)
        self.meeting.record = "결정: A안"
        self.meeting.transcript = "A안으로 진행합니다."
        with patch.dict(os.environ, {"GEMINI_API_KEY": "k1", "GEMINI_MODEL": "gemini-x"}, clear=True), \
                patch("urllib.request.urlopen", side_effect=error), patch.object(services.time, "sleep"):
            self.assertIn("AI 연결 실패", services.chat_reply(self.meeting, "q"))

    def test_gemini_retries_transient_error_then_succeeds(self):
        import urllib.error
        payload = {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}
        success = self._fake_gemini_urlopen({}, payload)
        calls = []

        def flaky_urlopen(request, timeout):
            calls.append(timeout)
            if len(calls) < 2:
                raise urllib.error.HTTPError("u", 503, "Service Unavailable", {}, None)
            return success(request, timeout)

        with patch.dict(os.environ, {"GEMINI_API_KEY": "k1", "GEMINI_MODEL": "gemini-x"}, clear=True), \
                patch("urllib.request.urlopen", side_effect=flaky_urlopen), patch.object(services.time, "sleep") as sleep:
            self.assertEqual(services._generate("hi"), "ok")
        self.assertEqual(len(calls), 2)
        self.assertEqual(sleep.call_count, 1)
        self.assertEqual(calls[0], services.GEMINI_DEFAULT_TIMEOUT_SECONDS)

    def test_gemini_is_tried_at_most_twice(self):
        import urllib.error
        error = urllib.error.HTTPError("u", 503, "Service Unavailable", {}, None)
        with patch.dict(os.environ, {"GEMINI_API_KEY": "k1", "GEMINI_MODEL": "gemini-x"}, clear=True), \
                patch("urllib.request.urlopen", side_effect=error) as urlopen, patch.object(services.time, "sleep"):
            self.assertRaises(OSError, services._generate, "hi")
        self.assertEqual(urlopen.call_count, 2)

    def test_gemini_daily_quota_429_is_not_retried(self):
        import urllib.error
        body = b'{"error": {"details": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}}'
        calls = []

        def quota_urlopen(request, timeout):
            calls.append(timeout)
            raise urllib.error.HTTPError("u", 429, "Too Many Requests", {}, io.BytesIO(body))

        with patch.dict(os.environ, {"GEMINI_API_KEY": "k1", "GEMINI_MODEL": "gemini-x"}, clear=True), \
                patch("urllib.request.urlopen", side_effect=quota_urlopen), patch.object(services.time, "sleep") as sleep:
            with self.assertRaises(OSError):
                services._generate("hi")
        self.assertEqual(len(calls), 1)
        sleep.assert_not_called()

    def test_gemini_does_not_retry_auth_error(self):
        import urllib.error
        error = urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)
        with patch.dict(os.environ, {"GEMINI_API_KEY": "k1", "GEMINI_MODEL": "gemini-x"}, clear=True), \
                patch("urllib.request.urlopen", side_effect=error) as urlopen, patch.object(services.time, "sleep") as sleep:
            self.assertRaises(OSError, services._generate, "hi")
        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()

    def test_gemini_timeout_is_retried_once(self):
        import urllib.error
        for error in (TimeoutError("timed out"), urllib.error.URLError(TimeoutError("connect timed out"))):
            with patch.dict(os.environ, {"GEMINI_API_KEY": "k1", "GEMINI_MODEL": "gemini-x"}, clear=True), \
                    patch("urllib.request.urlopen", side_effect=error) as urlopen, patch.object(services.time, "sleep") as sleep:
                self.assertRaises(OSError, services._generate, "hi")
            self.assertEqual(urlopen.call_count, 2)
            self.assertEqual(sleep.call_count, 1)

    def _gemini_down_ollama_up(self, urls):
        class FakeResponse:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return '{"response": "올라마 답"}'.encode("utf-8")

        def fake_urlopen(request, timeout):
            urls.append(request.full_url)
            if "generativelanguage" in request.full_url:
                raise TimeoutError("timed out")
            return FakeResponse()
        return fake_urlopen

    def test_gemini_is_skipped_for_a_while_after_failing(self):
        urls = []
        env = {"GEMINI_API_KEY": "k1", "GEMINI_MODEL": "gemini-x", "OLLAMA_MODEL": "m1"}
        with patch.dict(os.environ, env, clear=True), patch("urllib.request.urlopen", side_effect=self._gemini_down_ollama_up(urls)), \
                patch.object(services.time, "sleep"), self.assertLogs(services.logger, "INFO") as logs:
            self.assertEqual(services._generate("첫 호출"), "올라마 답")
            self.assertEqual(services._generate("두 번째 호출"), "올라마 답")
        # 첫 호출: Gemini 2번 실패 → Ollama. 두 번째 호출: Gemini를 건너뛰고 바로 Ollama.
        self.assertEqual([("generativelanguage" in url) for url in urls], [True, True, False, False])
        output = "\n".join(logs.output)
        self.assertIn("로컬 Ollama(m1)로 전환", output)
        self.assertIn("Gemini 일시 중단 중", output)
        self.assertIn("LLM 응답: 로컬 Ollama(m1)", output)

    def test_cooldown_zero_tries_gemini_every_time(self):
        urls = []
        env = {"GEMINI_API_KEY": "k1", "GEMINI_MODEL": "gemini-x", "OLLAMA_MODEL": "m1", "GEMINI_COOLDOWN": "0"}
        with patch.dict(os.environ, env, clear=True), patch("urllib.request.urlopen", side_effect=self._gemini_down_ollama_up(urls)), \
                patch.object(services.time, "sleep"):
            services._generate("첫 호출")
            services._generate("두 번째 호출")
        self.assertEqual(sum("generativelanguage" in url for url in urls), 4)

    def test_gemini_success_is_logged_with_model_name(self):
        payload = {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}
        with patch.dict(os.environ, {"GEMINI_API_KEY": "k1", "GEMINI_MODEL": "gemini-x"}, clear=True), \
                patch("urllib.request.urlopen", side_effect=self._fake_gemini_urlopen({}, payload)), \
                self.assertLogs(services.logger, "INFO") as logs:
            services._generate("hi")
        self.assertIn("LLM 응답: Gemini(gemini-x)", logs.output[0])

    def test_gemini_failure_falls_back_to_ollama_when_configured(self):
        import urllib.error
        error = urllib.error.HTTPError("u", 503, "Service Unavailable", {}, None)
        urls = []

        class FakeResponse:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return '{"response": "올라마 답"}'.encode("utf-8")

        def fake_urlopen(request, timeout):
            urls.append(request.full_url)
            if "generativelanguage" in request.full_url:
                raise error
            return FakeResponse()

        with patch.dict(os.environ, {"GEMINI_API_KEY": "k1", "GEMINI_MODEL": "gemini-x", "OLLAMA_MODEL": "m1"}, clear=True), \
                patch("urllib.request.urlopen", side_effect=fake_urlopen), patch.object(services.time, "sleep"):
            self.assertEqual(services._generate("hi"), "올라마 답")
        self.assertEqual(len(urls), services.GEMINI_RETRIES + 2)
        self.assertEqual(urls[-1], services.OLLAMA_URL)

    def test_gemini_failure_without_ollama_raises(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "k1", "GEMINI_MODEL": "gemini-x"}, clear=True), \
                patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")) as urlopen, patch.object(services.time, "sleep"):
            self.assertRaises(OSError, services._generate, "hi")
        self.assertEqual(urlopen.call_count, services.GEMINI_RETRIES + 1)
        # Ollama가 없으면 넘어갈 곳이 없으므로 건너뛰는 중이어도 다음 호출은 Gemini를 다시 시도한다.
        with patch.dict(os.environ, {"GEMINI_API_KEY": "k1", "GEMINI_MODEL": "gemini-x"}, clear=True), \
                patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")) as urlopen, patch.object(services.time, "sleep"):
            self.assertRaises(OSError, services._generate, "hi")
        self.assertEqual(urlopen.call_count, services.GEMINI_RETRIES + 1)

    def test_multiple_gemini_models_are_tried_in_order_then_ollama(self):
        import urllib.error
        urls = []

        class FakeResponse:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return '{"response": "올라마 답"}'.encode("utf-8")

        quota = b'{"error": {"details": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}}'

        def fake_urlopen(request, timeout):
            urls.append(request.full_url)
            if "gemini-a" in request.full_url:
                raise urllib.error.HTTPError("u", 429, "Too Many Requests", {}, io.BytesIO(quota))
            if "gemini-b" in request.full_url:
                raise TimeoutError("timed out")
            return FakeResponse()

        env = {"GEMINI_API_KEY": "k1", "GEMINI_MODEL": "gemini-a, gemini-b", "OLLAMA_MODEL": "m1"}
        with patch.dict(os.environ, env, clear=True), patch("urllib.request.urlopen", side_effect=fake_urlopen), \
                patch.object(services.time, "sleep"), self.assertLogs(services.logger, "INFO") as logs:
            self.assertEqual(services._generate("첫 호출"), "올라마 답")
            self.assertEqual(services._generate("두 번째 호출"), "올라마 답")
        # 첫 호출: a는 하루 한도라 한 번, b는 마지막 모델이라 시간 초과를 한 번 더 기다린 뒤 Ollama.
        # 두 번째 호출: 두 모델 모두 건너뛰는 중이라 바로 Ollama.
        called = ["a" if "gemini-a" in url else "b" if "gemini-b" in url else "ollama" for url in urls]
        self.assertEqual(called, ["a", "b", "b", "ollama", "ollama"])
        self.assertIn("Gemini(gemini-a) 실패", "\n".join(logs.output))
        self.assertIn("다음 모델 gemini-b 시도", "\n".join(logs.output))

    def test_next_gemini_model_answers_when_the_first_one_fails(self):
        import urllib.error
        payload = {"candidates": [{"content": {"parts": [{"text": "두 번째 모델 답"}]}}]}
        success = self._fake_gemini_urlopen({}, payload)
        urls = []

        def fake_urlopen(request, timeout):
            urls.append(request.full_url)
            if "gemini-a" in request.full_url:
                raise TimeoutError("timed out")
            return success(request, timeout)

        env = {"GEMINI_API_KEY": "k1", "GEMINI_MODEL": "gemini-a,gemini-b"}
        with patch.dict(os.environ, env, clear=True), patch("urllib.request.urlopen", side_effect=fake_urlopen), \
                patch.object(services.time, "sleep") as sleep, self.assertLogs(services.logger, "INFO") as logs:
            self.assertEqual(services._generate("hi"), "두 번째 모델 답")
        # 앞 모델은 시간 초과를 다시 기다리지 않고 바로 다음 모델로 넘어간다.
        self.assertEqual(len(urls), 2)
        sleep.assert_not_called()
        self.assertIn("LLM 응답: Gemini(gemini-b)", "\n".join(logs.output))

    def test_gemini_timeout_env_overrides_default(self):
        captured = {}
        payload = {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}
        fake = self._fake_gemini_urlopen(captured, payload)
        with patch.dict(os.environ, {"GEMINI_API_KEY": "k1", "GEMINI_MODEL": "gemini-x", "GEMINI_TIMEOUT": "42"}, clear=True), \
                patch("urllib.request.urlopen", side_effect=fake) as urlopen:
            services._generate("hi")
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 42)

    # ---- Groq(LLM) — Gemini와 Ollama 사이의 두 번째 단계 ----

    def _fake_groq_urlopen(self, captured, payload):
        class FakeResponse:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return json.dumps(payload).encode("utf-8")

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["headers"] = dict(request.header_items())
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse()
        return fake_urlopen

    def test_groq_llm_needs_both_api_key_and_model(self):
        with patch.dict(os.environ, {"GROQ_API_KEY": "k1", "GROQ_LLM_MODEL": "llama-x"}, clear=True):
            self.assertTrue(services._enabled())
        with patch.dict(os.environ, {"GROQ_API_KEY": "k1"}, clear=True):
            self.assertFalse(services._enabled())
        with patch.dict(os.environ, {"GROQ_LLM_MODEL": "llama-x"}, clear=True):
            self.assertFalse(services._enabled())

    def test_groq_llm_request_and_response(self):
        captured = {}
        payload = {"choices": [{"message": {"content": " 답변 "}}]}
        fake = self._fake_groq_urlopen(captured, payload)
        with patch.dict(os.environ, {"GROQ_API_KEY": "k1", "GROQ_LLM_MODEL": "llama-x"}, clear=True), \
                patch("urllib.request.urlopen", side_effect=fake):
            result = services._generate("hi", temperature=0.3)
        self.assertEqual(result, "답변")
        self.assertEqual(captured["url"], services.GROQ_CHAT_URL)
        self.assertEqual(captured["headers"].get("Authorization"), "Bearer k1")
        self.assertEqual(captured["body"]["messages"][0]["content"], "hi")
        self.assertEqual(captured["body"]["temperature"], 0.3)

    def test_gemini_failure_falls_back_to_groq(self):
        import urllib.error
        error = urllib.error.HTTPError("u", 503, "Service Unavailable", {}, None)
        urls = []

        class FakeResponse:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return json.dumps({"choices": [{"message": {"content": "그록 답"}}]}).encode("utf-8")

        def fake_urlopen(request, timeout):
            urls.append(request.full_url)
            if "generativelanguage" in request.full_url:
                raise error
            return FakeResponse()

        with patch.dict(os.environ, {
            "GEMINI_API_KEY": "k1", "GEMINI_MODEL": "gemini-x", "GROQ_API_KEY": "k2", "GROQ_LLM_MODEL": "llama-x",
        }, clear=True), patch("urllib.request.urlopen", side_effect=fake_urlopen), patch.object(services.time, "sleep"):
            self.assertEqual(services._generate("hi"), "그록 답")
        self.assertEqual(urls[-1], services.GROQ_CHAT_URL)

    def test_groq_llm_failure_falls_back_to_ollama_when_configured(self):
        import urllib.error
        error = urllib.error.HTTPError("u", 503, "Service Unavailable", {}, None)
        urls = []

        class FakeResponse:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return '{"response": "올라마 답"}'.encode("utf-8")

        def fake_urlopen(request, timeout):
            urls.append(request.full_url)
            if request.full_url == services.GROQ_CHAT_URL:
                raise error
            return FakeResponse()

        with patch.dict(os.environ, {
            "GROQ_API_KEY": "k1", "GROQ_LLM_MODEL": "llama-x", "OLLAMA_MODEL": "m1",
        }, clear=True), patch("urllib.request.urlopen", side_effect=fake_urlopen):
            self.assertEqual(services._generate("hi"), "올라마 답")
        self.assertEqual(urls[-1], services.OLLAMA_URL)

    def test_groq_llm_failure_without_ollama_raises(self):
        with patch.dict(os.environ, {"GROQ_API_KEY": "k1", "GROQ_LLM_MODEL": "llama-x"}, clear=True), \
                patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")) as urlopen:
            self.assertRaises(OSError, services._generate, "hi")
        self.assertEqual(urlopen.call_count, 1)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(), BACKGROUND_RECORDING=False)


class StuckAndFileAccessTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("a", password="pw12345!")
        self.client.force_login(self.user)
        self.team = Team.objects.create(name="t")
        self.team.members.add(self.user)
        self.meeting = Meeting.objects.create(team=self.team, title="m", host=self.user)

    def test_processing_without_live_worker_is_marked_failed(self):
        rec = Recording.objects.create(meeting=self.meeting, file=SimpleUploadedFile("a.wav", b"RIFF"), status=Recording.STATUS_PROCESSING)
        self.meeting.processing_status = Meeting.STATUS_PROCESSING
        self.meeting.save()
        r = self.client.get(reverse("meeting_result", args=[self.meeting.id]))
        self.assertContains(r, "녹음 처리에 실패했습니다.")
        rec.refresh_from_db()
        self.assertEqual(rec.status, Recording.STATUS_FAILED)
        self.meeting.refresh_from_db()
        self.assertEqual(self.meeting.processing_status, Meeting.STATUS_FAILED)

    def test_processing_with_live_worker_is_left_alone(self):
        rec = Recording.objects.create(meeting=self.meeting, file=SimpleUploadedFile("a.wav", b"RIFF"), status=Recording.STATUS_PROCESSING)
        self.meeting.processing_status = Meeting.STATUS_PROCESSING
        self.meeting.save()
        tasks._active.add(rec.id)
        self.addCleanup(tasks._active.discard, rec.id)
        self.client.get(reverse("meeting_result", args=[self.meeting.id]))
        rec.refresh_from_db()
        self.assertEqual(rec.status, Recording.STATUS_PROCESSING)

    def _make_recording(self, name, content):
        return Recording.objects.create(meeting=self.meeting, file=SimpleUploadedFile(name, content), status=Recording.STATUS_DONE)

    def test_recording_download_requires_login(self):
        rec = self._make_recording("a.wav", b"RIFF")
        self.client.logout()
        r = self.client.get(reverse("recording_file", args=[self.meeting.id, rec.id]))
        self.assertEqual(r.status_code, 302)

    def test_member_can_download_recording(self):
        rec = self._make_recording("a.wav", b"RIFFDATA")
        r = self.client.get(reverse("recording_file", args=[self.meeting.id, rec.id]))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(b"".join(r.streaming_content), b"RIFFDATA")

    def test_outsider_cannot_download_recording(self):
        rec = self._make_recording("a.wav", b"RIFF")
        outsider = User.objects.create_user("c", password="pw12345!")
        client = Client()
        client.force_login(outsider)
        self.assertEqual(client.get(reverse("recording_file", args=[self.meeting.id, rec.id])).status_code, 404)

    def test_unknown_recording_returns_404(self):
        self.assertEqual(self.client.get(reverse("recording_file", args=[self.meeting.id, 99999])).status_code, 404)

    def test_retry_button_shown_only_for_failed_recordings(self):
        self._make_recording("done.wav", b"R")
        failed = Recording.objects.create(meeting=self.meeting, file=SimpleUploadedFile("f.wav", b"R"), status=Recording.STATUS_FAILED)
        r = self.client.get(reverse("meeting_detail", args=[self.meeting.id]))
        self.assertContains(r, "다시 시도")
        self.assertEqual(r.content.decode().count("다시 시도"), 1)

    def test_retry_resets_failed_recording_and_reprocesses(self):
        failed = Recording.objects.create(meeting=self.meeting, file=SimpleUploadedFile("f.wav", b"R"), status=Recording.STATUS_FAILED)
        with patch.object(tasks, "start_processing") as start:
            r = self.client.post(reverse("recording_retry", args=[self.meeting.id, failed.id]))
        self.assertEqual(r.status_code, 302)
        failed.refresh_from_db()
        self.assertEqual(failed.status, Recording.STATUS_PROCESSING)
        start.assert_called_once_with(failed.id)

    def test_cannot_retry_a_recording_that_is_not_failed(self):
        done = self._make_recording("d.wav", b"R")
        r = self.client.post(reverse("recording_retry", args=[self.meeting.id, done.id]))
        self.assertEqual(r.status_code, 404)

    def test_outsider_cannot_retry_recording(self):
        failed = Recording.objects.create(meeting=self.meeting, file=SimpleUploadedFile("f.wav", b"R"), status=Recording.STATUS_FAILED)
        outsider = User.objects.create_user("d", password="pw12345!")
        client = Client()
        client.force_login(outsider)
        self.assertEqual(client.post(reverse("recording_retry", args=[self.meeting.id, failed.id])).status_code, 404)


class DesignAlignmentTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("a", password="pw12345!")
        self.client.force_login(self.user)
        self.team = Team.objects.create(name="t")
        self.team.members.add(self.user)
        self.meeting = Meeting.objects.create(team=self.team, title="m", host=self.user, record="결정: A안")

    def test_record_download_returns_text_for_members(self):
        r = self.client.get(reverse("record_file", args=[self.meeting.id]))
        self.assertEqual(r.status_code, 200)
        self.assertIn("attachment", r["Content-Disposition"])
        self.assertEqual(r.content.decode("utf-8"), "결정: A안")

    def test_record_download_404_when_empty_or_outsider(self):
        self.meeting.record = ""
        self.meeting.save()
        self.assertEqual(self.client.get(reverse("record_file", args=[self.meeting.id])).status_code, 404)
        self.meeting.record = "결정"
        self.meeting.save()
        outsider = User.objects.create_user("c", password="pw12345!")
        client = Client()
        client.force_login(outsider)
        self.assertEqual(client.get(reverse("record_file", args=[self.meeting.id])).status_code, 404)

    def test_result_page_shows_download_button_when_done(self):
        self.meeting.processing_status = Meeting.STATUS_DONE
        self.meeting.save()
        r = self.client.get(reverse("meeting_result", args=[self.meeting.id]))
        self.assertContains(r, "회의록 다운로드")
        self.assertContains(r, reverse("record_file", args=[self.meeting.id]))

    def test_main_page_banner_lists_meetings_due_tomorrow(self):
        self.meeting.deadline = timezone.localdate() + timedelta(days=1)
        self.meeting.save()
        r = self.client.get(reverse("index"))
        self.assertContains(r, "내일 마감인 회의")
        self.assertContains(r, self.meeting.title)

    def test_main_page_has_no_banner_when_not_due(self):
        r = self.client.get(reverse("index"))
        self.assertNotContains(r, "내일 마감인 회의")


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(), BACKGROUND_RECORDING=False)


class FollowupTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("a", password="pw12345!")
        self.other = User.objects.create_user("b", password="pw12345!")
        self.client.force_login(self.user)
        self.team = Team.objects.create(name="t")
        self.team.members.add(self.user, self.other)
        self.meeting = Meeting.objects.create(team=self.team, title="m", host=self.user, record="결정: A안")

    def test_second_upload_keeps_the_first_recording(self):
        self.client.post(reverse("meeting_detail", args=[self.meeting.id]), {"action": "submit", "recording": SimpleUploadedFile("a.wav", b"RIFF1")})
        self.client.post(reverse("meeting_detail", args=[self.meeting.id]), {"action": "submit", "recording": SimpleUploadedFile("b.wav", b"RIFF2")})
        recs = list(Recording.objects.filter(meeting=self.meeting))
        self.assertEqual(len(recs), 2)
        self.assertTrue(all(os.path.exists(r.file.path) for r in recs))


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(), BACKGROUND_RECORDING=False)


class MultipleRecordingsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("a", password="pw12345!")
        self.client.force_login(self.user)
        self.team = Team.objects.create(name="t")
        self.team.members.add(self.user)
        self.meeting = Meeting.objects.create(team=self.team, title="m", host=self.user)

    def test_transcripts_of_all_recordings_are_joined_in_order(self):
        first = Recording.objects.create(meeting=self.meeting, file=SimpleUploadedFile("1.wav", b"R"), status=Recording.STATUS_PROCESSING)
        second = Recording.objects.create(meeting=self.meeting, file=SimpleUploadedFile("2.wav", b"R"), status=Recording.STATUS_PROCESSING)
        with patch.object(services, "transcribe", side_effect=["앞부분 전사", "뒷부분 전사"]):
            tasks.process_recording(first.id)
            tasks.process_recording(second.id)
        self.meeting.refresh_from_db()
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(
            self.meeting.transcript,
            "[%s]\n앞부분 전사\n\n[%s]\n뒷부분 전사" % (first.filename, second.filename),
        )
        self.assertEqual(self.meeting.processing_status, Meeting.STATUS_DONE)

    def test_meeting_stays_processing_while_any_recording_is_running(self):
        done = Recording.objects.create(meeting=self.meeting, file=SimpleUploadedFile("1.wav", b"R"), status=Recording.STATUS_PROCESSING)
        Recording.objects.create(meeting=self.meeting, file=SimpleUploadedFile("2.wav", b"R"), status=Recording.STATUS_PROCESSING)
        with patch.object(services, "transcribe", return_value="끝난 전사"):
            tasks.process_recording(done.id)
        self.meeting.refresh_from_db()
        self.assertEqual(self.meeting.processing_status, Meeting.STATUS_PROCESSING)

    def test_failed_recording_does_not_block_others(self):
        bad = Recording.objects.create(meeting=self.meeting, file=SimpleUploadedFile("1.wav", b"R"), status=Recording.STATUS_PROCESSING)
        good = Recording.objects.create(meeting=self.meeting, file=SimpleUploadedFile("2.wav", b"R"), status=Recording.STATUS_PROCESSING)
        with patch.object(services, "transcribe", side_effect=[RuntimeError("boom"), "좋은 전사"]):
            tasks.process_recording(bad.id)
            tasks.process_recording(good.id)
        self.meeting.refresh_from_db()
        self.assertEqual(self.meeting.transcript, "좋은 전사")
        self.assertEqual(self.meeting.processing_status, Meeting.STATUS_DONE)


class DeleteTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("a", password="pw12345!")
        self.other = User.objects.create_user("b", password="pw12345!")
        self.client.force_login(self.user)
        self.team = Team.objects.create(name="t", created_by=self.user)
        self.team.members.add(self.user, self.other)
        self.meeting = Meeting.objects.create(team=self.team, title="m", host=self.user)

    def test_host_can_delete_meeting(self):
        r = self.client.post(reverse("meeting_detail", args=[self.meeting.id]), {"action": "delete_meeting"})
        self.assertRedirects(r, reverse("meeting_list", args=[self.team.id]))
        self.assertFalse(Meeting.objects.filter(pk=self.meeting.id).exists())

    def test_non_host_cannot_delete_meeting(self):
        client = Client()
        client.force_login(self.other)
        r = client.post(reverse("meeting_detail", args=[self.meeting.id]), {"action": "delete_meeting"})
        self.assertEqual(r.status_code, 403)
        self.assertTrue(Meeting.objects.filter(pk=self.meeting.id).exists())

    def test_creator_can_delete_team(self):
        r = self.client.post(reverse("meeting_list", args=[self.team.id]), {"action": "delete_team"})
        self.assertRedirects(r, reverse("index"))
        self.assertFalse(Team.objects.filter(pk=self.team.id).exists())

    def test_non_creator_member_cannot_delete_team(self):
        client = Client()
        client.force_login(self.other)
        r = client.post(reverse("meeting_list", args=[self.team.id]), {"action": "delete_team"})
        self.assertEqual(r.status_code, 403)
        self.assertTrue(Team.objects.filter(pk=self.team.id).exists())

    def test_meeting_list_shows_delete_button_only_to_creator(self):
        r = self.client.get(reverse("meeting_list", args=[self.team.id]))
        self.assertContains(r, "팀 삭제")
        client = Client()
        client.force_login(self.other)
        r = client.get(reverse("meeting_list", args=[self.team.id]))
        self.assertNotContains(r, "팀 삭제")

    def test_meeting_detail_shows_delete_button_only_to_host(self):
        r = self.client.get(reverse("meeting_detail", args=[self.meeting.id]))
        self.assertContains(r, "회의 삭제")
        client = Client()
        client.force_login(self.other)
        r = client.get(reverse("meeting_detail", args=[self.meeting.id]))
        self.assertNotContains(r, "회의 삭제")


class DecisionWarningTests(TestCase):
    """규칙 안전망. AI가 '명확'이라 한 항목의 확정 문장과 원문 근거만 되짚는다.

    빈칸은 더 이상 사유가 아니고(빈칸만으로는 묻지 않는다), 모호 판정 자체는 AI가 한다.
    DB도 LLM도 필요 없는 순수 함수 테스트다.
    """

    # ---- safety_warnings: AI가 '명확'이라 한 항목만 되짚는 안전망 ----

    def test_safety_catches_an_ambiguous_phrase_in_the_sentence(self):
        self.assertEqual(
            decision_compiler.safety_warnings("나중에 로깅 기준을 맞춘다", "로깅은 맞춰야죠"),
            ["확정 문장에 모호한 표현이 있어요: '나중에'"],
        )

    def test_safety_catches_an_ambiguous_phrase_only_in_the_transcript(self):
        """원문은 모호한데 AI가 혼자 해소한 경우 — 회의에서 정해지지 않은 것이므로 묻는다."""
        self.assertEqual(
            decision_compiler.safety_warnings("민수가 다음 주 금요일까지 배포한다", "다음 주쯤 할게요"),
            ["원문에 모호한 표현이 있어요: '쯤'"],
        )

    def test_safety_lists_the_same_phrase_once(self):
        self.assertEqual(
            decision_compiler.safety_warnings("나중에 맞춘다", "나중에 맞추죠"),
            ["확정 문장에 모호한 표현이 있어요: '나중에'"],
        )

    def test_safety_catches_conflicting_dates(self):
        self.assertEqual(
            decision_compiler.safety_warnings("금요일 아니면 월요일에 배포한다", ""),
            ["확정 문장에 다른 날짜가 섞여 있어요: 금요일, 월요일"],
        )

    def test_safety_does_not_split_a_single_date(self):
        self.assertEqual(decision_compiler.safety_warnings("12월 5일까지 배포한다", ""), [])

    def test_safety_reads_a_day_and_a_time_as_one_deadline(self):
        """"금요일 18시까지"는 기한 하나다 — 날짜와 시각을 섞어 세면 거의 모든 문장이 걸린다."""
        self.assertEqual(
            decision_compiler.safety_warnings("민수가 금요일 18시까지 배포 스크립트를 정리한다", ""),
            [],
        )

    def test_safety_catches_two_different_times(self):
        self.assertEqual(
            decision_compiler.safety_warnings("14시 아니면 18시에 배포한다", ""),
            ["확정 문장에 다른 시각이 섞여 있어요: 14시, 18시"],
        )

    def test_safety_reads_a_deadline_and_a_duration_as_one_sentence(self):
        """"12월 5일까지 … 30일로"는 기한 하나와 기간 하나다 — 날짜 충돌이 아니다.

        확정 문장은 거의 항상 "언제까지 + 얼마나"를 함께 담으므로, 이것을 충돌로 세면
        빈칸 경고를 없앤 자리에 같은 노이즈가 다시 들어온다.
        """
        self.assertEqual(
            decision_compiler.safety_warnings(
                "민수가 12월 5일까지 로그 보관 기간을 30일로 바꾼다", ""),
            [],
        )

    def test_safety_does_not_count_a_round_or_a_month_span_as_a_quantity(self):
        self.assertEqual(
            decision_compiler.safety_warnings(
                "영희가 12월 8일까지 화면 2개를 QA하고 1차 결과를 공유한다", ""),
            [],
        )
        self.assertEqual(
            decision_compiler.safety_warnings(
                "민수가 12월 5일까지 감사 로그 3개월 요건을 문서에 남긴다", ""),
            [],
        )

    def test_safety_catches_conflicting_quantities(self):
        self.assertEqual(
            decision_compiler.safety_warnings("테스트 3개를 쓰고 5개를 통과시킨다", ""),
            ["확정 문장에 다른 숫자가 섞여 있어요: 3개, 5개"],
        )

    # ---- 확정 문장에 남은 지시어(REFERENT_PHRASES) ----

    def test_safety_catches_a_referent_left_in_the_final_text(self):
        """담당자는 … 해당 분에게 넘긴다"처럼, 확정 문장이 실제로 이름을 못 정한 경우."""
        self.assertEqual(
            decision_compiler.safety_warnings("담당자는 그쪽에서 받은 파일을 넘긴다", "어제 그쪽에서 보내준 파일"),
            ["확정 문장에 모호한 표현이 있어요: '그쪽'"],
        )

    def test_safety_ignores_a_referent_that_only_appears_in_the_transcript(self):
        """원문(자연스러운 대화체)에 지시어가 있어도, 확정 문장이 깔끔하면 걸리지 않는다."""
        self.assertEqual(
            decision_compiler.safety_warnings("민수가 금요일까지 배포한다", "그쪽에서 얘기한 대로 민수가 금요일까지 배포하기로 했어요"),
            [],
        )


class DecisionParseTests(TestCase):
    """통합 응답 파서(parse_items)의 스키마 검증과 근거 검증."""

    # ---- parse_items: 결정·모호 여부·이유·후보·근거·추천을 한 응답에서 읽는다 ----

    TOPIC = "로그 보관 기간"
    CANDIDATES = [
        {"text": "민수가 30일로 바꾼다", "evidence": "", "recommended": True},
        {"text": "민수가 90일로 바꾼다", "evidence": ""},
    ]

    def test_parse_items_reads_the_new_schema(self):
        raw = json.dumps([
            {"topic": self.TOPIC, "ambiguous": True, "why": "30일인지 90일인지 갈립니다",
             "final": "무시되어야 한다", "evidence": "한 달 정도면 충분해요", "candidate": "민수가 30일로 바꾼다"},
            {"topic": "배포", "ambiguous": False, "final": "민수가 금요일까지 배포한다",
             "evidence": "민수가 금요일까지 배포를 맡기로 했습니다.", "candidate": "무시되어야 한다"},
        ], ensure_ascii=False)
        items = decision_compiler.parse_items(raw)
        self.assertEqual(items[0]["why"], "30일인지 90일인지 갈립니다")
        # AI 추천 문장은 하나이고, 생성 문장에는 근거를 붙이지 않는다.
        self.assertEqual(items[0]["candidates"], [{"text": "민수가 30일로 바꾼다", "evidence": "", "recommended": True}])
        # 근거는 결정 하나에 하나다.
        self.assertEqual(items[0]["evidence"], "한 달 정도면 충분해요")
        # 모호 항목에 final이 함께 와도 추천 문장만 쓴다 — 기준이 둘이 되지 않게.
        self.assertEqual(items[0]["final"], "")
        self.assertEqual(items[1]["candidates"], [])
        self.assertEqual(items[1]["final"], "민수가 금요일까지 배포한다")
        self.assertEqual(items[1]["why"], "")

    def test_parse_items_keeps_only_one_sentence_from_an_old_style_list(self):
        """모델이 지시를 어기고 후보 여러 개를 보내도 하나만 남긴다 — 추천이 붙은 것이 먼저다."""
        raw = json.dumps([{"topic": self.TOPIC, "ambiguous": True, "why": "x", "candidates": [
            {"text": "첫째"}, {"text": "둘째", "recommended": True}, "셋째",
        ]}], ensure_ascii=False)
        self.assertEqual([c["text"] for c in decision_compiler.parse_items(raw)[0]["candidates"]], ["둘째"])
        raw = json.dumps([{"topic": self.TOPIC, "ambiguous": True, "why": "x", "candidates": ["문장만 왔다", "이것도"]}],
                         ensure_ascii=False)
        self.assertEqual([c["text"] for c in decision_compiler.parse_items(raw)[0]["candidates"]], ["문장만 왔다"])

    def test_old_style_candidate_evidence_becomes_the_decisions_evidence(self):
        source = "로그는 한 달 정도면 충분해요."
        raw = json.dumps([{"topic": self.TOPIC, "ambiguous": True, "why": "x", "candidates": [
            {"text": "첫째", "evidence": "회의에 없던 문장"},
            {"text": "둘째", "evidence": "로그는 한 달 정도면 충분해요"},
        ]}], ensure_ascii=False)
        self.assertEqual(decision_compiler.parse_items(raw, source_text=source)[0]["evidence"], "로그는 한 달 정도면 충분해요")

    def test_parse_items_drops_items_without_a_topic(self):
        raw = json.dumps([{"topic": "", "ambiguous": False, "final": "문장"},
                          {"topic": "가" * 300, "ambiguous": False, "final": "문장"}],
                         ensure_ascii=False)
        items = decision_compiler.parse_items(raw)
        self.assertEqual(len(items), 1)
        # 긴 안건 이름은 카드 제목이 레이아웃을 깨지 않게 잘라 둔다.
        self.assertEqual(len(items[0]["topic"]), 120)

    def test_parse_items_survives_code_fences_and_prose(self):
        raw = "네.\n```json\n[{\"topic\": \"배포\", \"ambiguous\": false, \"final\": \"문장\"}]\n```\n이상."
        self.assertEqual(len(decision_compiler.parse_items(raw)), 1)

    def test_parse_items_returns_empty_on_broken_json(self):
        for raw in ("JSON 못 만들겠습니다", '[{"topic": "배포",]', '{"topic": "배포"}'):
            self.assertEqual(decision_compiler.parse_items(raw), [], raw)

    def test_parse_items_caps_the_number_of_items(self):
        raw = json.dumps([{"topic": "안건 %d" % i, "ambiguous": False, "final": "문장"}
                          for i in range(30)], ensure_ascii=False)
        self.assertEqual(len(decision_compiler.parse_items(raw)), decision_compiler.MAX_DECISIONS)

    def test_parse_items_skips_blank_sentences(self):
        raw = json.dumps([{"topic": self.TOPIC, "ambiguous": True, "why": "x", "candidates": [
            {"text": ""}, {"text": "같은 후보"}, {"text": "같은 후보"},
        ]}], ensure_ascii=False)
        self.assertEqual([c["text"] for c in decision_compiler.parse_items(raw)[0]["candidates"]],
                         ["같은 후보"])

    def test_parse_items_drops_a_candidate_that_still_has_the_ambiguous_phrase(self):
        """모호함을 없애라고 만든 후보가 그 모호어를 그대로 되풀이하면 고를 의미가 없다."""
        raw = json.dumps([{"topic": self.TOPIC, "ambiguous": True, "why": "x", "candidates": [
            {"text": "테스트 커버리지를 적당히 올린다", "evidence": ""},
            {"text": "테스트 커버리지를 80퍼센트로 올린다", "evidence": ""},
        ]}], ensure_ascii=False)
        self.assertEqual([c["text"] for c in decision_compiler.parse_items(raw)[0]["candidates"]],
                         ["테스트 커버리지를 80퍼센트로 올린다"])

    def test_parse_items_drops_a_candidate_that_still_has_an_unresolved_referent(self):
        raw = json.dumps([{"topic": self.TOPIC, "ambiguous": True, "why": "x", "candidates": [
            {"text": "문서를 그쪽에 넘긴다", "evidence": ""},
        ]}], ensure_ascii=False)
        item = decision_compiler.parse_items(raw)[0]
        self.assertEqual(item["candidates"], [])
        # 버린 후보를 통째로 잃지 않는다 — 모호한 부분만 빈칸으로 바꿔 draft_hint로 남긴다.
        self.assertEqual(item["draft_hint"], "문서를 (구체적으로 적기)에 넘긴다")

    def test_parse_items_draft_hint_is_empty_when_no_candidate_was_dropped(self):
        raw = json.dumps([{"topic": self.TOPIC, "ambiguous": True, "why": "x", "candidates": [
            {"text": "80퍼센트로 올린다", "evidence": ""},
        ]}], ensure_ascii=False)
        self.assertEqual(decision_compiler.parse_items(raw)[0]["draft_hint"], "")

    def test_parse_items_draft_hint_keeps_only_the_first_dropped_candidate(self):
        raw = json.dumps([{"topic": self.TOPIC, "ambiguous": True, "why": "x", "candidates": [
            {"text": "그쪽에 전달한다", "evidence": ""},
            {"text": "그분한테 전달한다", "evidence": ""},
        ]}], ensure_ascii=False)
        self.assertEqual(decision_compiler.parse_items(raw)[0]["draft_hint"], "(구체적으로 적기)에 전달한다")

    def test_parse_items_caps_candidates(self):
        raw = json.dumps([{"topic": self.TOPIC, "ambiguous": True, "why": "x",
                           "candidates": [{"text": "후보 %d" % i} for i in range(8)]}],
                         ensure_ascii=False)
        self.assertEqual(len(decision_compiler.parse_items(raw)[0]["candidates"]),
                         decision_compiler.MAX_INTERPRETATIONS)

    def test_the_suggested_sentence_is_kept_even_if_the_evidence_is_not_in_the_transcript(self):
        """생성한 문장은 근거 검증으로 버리지 않는다 — 원문에 없는 근거는 하이라이트만 못 할 뿐이다."""
        source = "민수가 금요일까지 배포를 맡기로 했습니다."
        raw = json.dumps([{"topic": self.TOPIC, "ambiguous": True, "why": "x",
                           "evidence": "회의에 없던 문장입니다.", "candidate": "남는다"}], ensure_ascii=False)
        item = decision_compiler.parse_items(raw, source_text=source)[0]
        self.assertEqual([c["text"] for c in item["candidates"]], ["남는다"])
        self.assertEqual(item["evidence"], "")

    def test_parse_items_matches_evidence_loosely(self):
        source = "민수가 금요일까지 배포를 맡기로 했습니다."
        raw = json.dumps([{"topic": self.TOPIC, "ambiguous": True, "why": "x",
                           "evidence": "민수가  금요일까지\n배포를 맡기로", "candidate": "공백 무시"}], ensure_ascii=False)
        item = decision_compiler.parse_items(raw, source_text=source)[0]
        self.assertEqual(item["evidence"], "민수가  금요일까지\n배포를 맡기로")

    def test_parse_items_requires_evidence_for_a_clear_item(self):
        """근거가 아예 없는 확정 문장은 검증을 건너뛰지 않고 비운다.

        후보 쪽의 빈 근거는 가짜 응답 모드 통로로 남기지만(바로 아래 테스트), 녹취 원문을
        들고 검증하는 중이라면 근거 없는 문장은 "원문에 있다"고 볼 수 없다.
        """
        raw = json.dumps([{"topic": self.TOPIC, "ambiguous": False,
                           "final": "민수가 30일로 바꾼다"}], ensure_ascii=False)
        self.assertEqual(decision_compiler.parse_items(raw, source_text="로그 이야기")[0]["final"], "")
        # 원문을 들고 있지 않을 때(가짜 응답 모드)는 그대로 둔다.
        self.assertEqual(decision_compiler.parse_items(raw)[0]["final"], "민수가 30일로 바꾼다")

    def test_parse_items_clears_a_final_whose_evidence_is_not_in_the_transcript(self):
        raw = json.dumps([{"topic": self.TOPIC, "ambiguous": False,
                           "final": "민수가 30일로 바꾼다", "evidence": "회의에 없던 문장"}],
                         ensure_ascii=False)
        item = decision_compiler.parse_items(raw, source_text="로그 이야기")[0]
        self.assertEqual(item["final"], "")

    # ---- 중복 제거: topic과 근거가 둘 다 같을 때만 같은 안건으로 본다 ----

    def test_same_topic_and_evidence_are_deduped(self):
        """실제로 겪은 버그: 같은 안건을 문구만 살짝 바꿔 두 번 만들어내는 경우."""
        source = "새 기능은 적당히 빠르면 됩니다."
        raw = json.dumps([
            {"topic": "새로 추가하는 기능", "ambiguous": True, "why": "x", "evidence": source, "candidate": "이번 주 안에 완료"},
            {"topic": "새로 추가하는 기능", "ambiguous": True, "why": "y", "evidence": source, "candidate": "다음 주 안에 완료"},
        ], ensure_ascii=False)
        items = decision_compiler.parse_items(raw, source_text=source)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["candidates"][0]["text"], "이번 주 안에 완료")

    def test_different_topics_sharing_one_evidence_sentence_are_kept_separate(self):
        """한 문장에서 서로 다른 결정 두 개가 나올 수 있다 — topic까지 같아야 중복으로 본다."""
        source = "민수가 로그는 30일로 줄이고 QA는 다음 주까지 진행합니다."
        raw = json.dumps([
            {"topic": "로그 보관 기간", "ambiguous": False, "final": "로그는 30일로 줄인다",
             "evidence": source},
            {"topic": "QA 일정", "ambiguous": False, "final": "QA는 다음 주까지 진행한다",
             "evidence": source},
        ], ensure_ascii=False)
        items = decision_compiler.parse_items(raw, source_text=source)
        self.assertEqual(len(items), 2)

    def test_same_topic_with_different_evidence_is_not_deduped(self):
        """이름만 우연히 같은 서로 다른 안건까지 합치면 안 된다."""
        raw = json.dumps([
            {"topic": self.TOPIC, "ambiguous": False, "final": "x", "evidence": "문장 하나"},
            {"topic": self.TOPIC, "ambiguous": False, "final": "y", "evidence": "다른 문장"},
        ], ensure_ascii=False)
        items = decision_compiler.parse_items(raw, source_text="문장 하나 다른 문장")
        self.assertEqual(len(items), 2)

    def test_items_with_blank_evidence_are_never_deduped_even_with_the_same_topic(self):
        """근거가 비어 있으면(가짜 응답 모드 등) topic이 같아도 중복으로 보지 않는다."""
        raw = json.dumps([
            {"topic": "안건1", "ambiguous": True, "why": "x",
             "candidates": [{"text": "후보", "evidence": ""}]},
            {"topic": "안건1", "ambiguous": True, "why": "y",
             "candidates": [{"text": "후보2", "evidence": ""}]},
        ], ensure_ascii=False)
        items = decision_compiler.parse_items(raw)
        self.assertEqual(len(items), 2)


class DecisionCompileTests(TestCase):
    """한 번의 AI 호출로 결정·모호 여부·후보를 받아 저장하는 전체 흐름.

    LLM 유무, 파싱 실패 재시도, 규칙 안전망의 승격, 가짜 응답 모드를 본다.
    """

    SOURCE = ("로그는 일단 한 달 정도면 충분할 것 같은데요. "
              "배포 스크립트는 제가 금요일까지 정리할게요. "
              "QA는 다음 주쯤 할게요.")
    CLEAR = {"topic": "배포 스크립트 정리", "ambiguous": False,
             "final": "민수가 금요일까지 배포 스크립트를 정리한다",
             "evidence": "배포 스크립트는 제가 금요일까지 정리할게요."}
    AMBIGUOUS = {"topic": "로그 보관 기간", "ambiguous": True,
                 "why": "30일인지 90일인지 갈립니다",
                 "evidence": "로그는 일단 한 달 정도면 충분할 것 같은데요.",
                 "candidate": "민수가 30일로 바꾼다"}

    def setUp(self):
        self.host = User.objects.create_user("host", password="pw12345!")
        self.team = Team.objects.create(name="t")
        self.team.members.add(self.host)
        self.meeting = Meeting.objects.create(
            team=self.team, title="m", host=self.host,
            processing_status=Meeting.STATUS_DONE, transcript=self.SOURCE,
        )

    def _llm(self, *responses):
        return patch.object(services, "_generate", side_effect=list(responses))

    def _compiled(self, *items):
        """한 응답으로 추출하고 (요약, {안건: Decision}, 호출 mock)을 돌려준다."""
        raw = json.dumps(list(items), ensure_ascii=False)
        with patch.dict(os.environ, {"OLLAMA_MODEL": "m1"}), \
             patch.object(services, "_generate", return_value=raw) as gen:
            summary = decision_compiler.compile_meeting(self.meeting)
        return summary, {d.what: d for d in self.meeting.decisions.all()}, gen

    # ---- 호출 한 번 ----

    def test_the_llm_is_called_exactly_once(self):
        _, _, gen = self._compiled(self.CLEAR, self.AMBIGUOUS)
        self.assertEqual(gen.call_count, 1)

    def test_transcript_is_sent_in_the_compile_prompt(self):
        _, _, gen = self._compiled(self.CLEAR)
        self.assertIn(self.meeting.transcript, gen.call_args_list[0][0][0])

    def test_compile_uses_the_llm_response_when_a_model_is_configured(self):
        _, by_topic, _ = self._compiled(self.CLEAR, self.AMBIGUOUS)
        self.assertEqual(sorted(by_topic), ["로그 보관 기간", "배포 스크립트 정리"])

    # ---- 명확한 결정: 탭 없이 공개 ----

    def test_a_clear_item_is_published_without_a_tap(self):
        _, by_topic, _ = self._compiled(self.CLEAR)
        decision = by_topic["배포 스크립트 정리"]
        self.assertEqual(decision.final_text, "민수가 금요일까지 배포 스크립트를 정리한다")
        self.assertEqual(decision.interpretations.count(), 0)
        self.assertEqual(decision.warnings, [])
        self.assertEqual(decision.divergence_reason, "")
        self.assertFalse(decision.blocks_publish)
        # 추출 필드는 더 이상 채우지 않는다 — 공개되는 것은 final_text 하나다.
        self.assertEqual(decision.who, "")
        self.assertEqual(decision.due_text, "")
        self.assertEqual(decision.done_criteria, "")

    # ---- 모호한 결정: 진행자가 탭해야 공개 ----

    def test_an_ambiguous_item_waits_for_the_host(self):
        _, by_topic, _ = self._compiled(self.AMBIGUOUS)
        decision = by_topic["로그 보관 기간"]
        self.assertEqual(decision.divergence_reason, "30일인지 90일인지 갈립니다")
        self.assertEqual(decision.final_text, "")
        self.assertTrue(decision.blocks_publish)
        # AI가 가장 추천하는 문장 하나만, 근거 없이 저장된다. 근거는 결정에 붙는다.
        self.assertEqual([(i.text, i.evidence, i.recommended) for i in decision.interpretations.all()],
                         [("민수가 30일로 바꾼다", "", True)])
        self.assertEqual(decision.evidence, "로그는 일단 한 달 정도면 충분할 것 같은데요.")

    # ---- 규칙 안전망의 승격 ----

    def test_a_rule_hit_on_the_sentence_promotes_a_clear_item(self):
        _, by_topic, _ = self._compiled(dict(self.CLEAR, final="나중에 배포 스크립트를 정리한다"))
        decision = by_topic["배포 스크립트 정리"]
        self.assertEqual(decision.warnings, ["확정 문장에 모호한 표현이 있어요: '나중에'"])
        # AI가 쓴 문장은 "이 문장 그대로 공개" 후보 하나로 올라간다.
        self.assertEqual([i.text for i in decision.interpretations.all()],
                         ["나중에 배포 스크립트를 정리한다"])
        self.assertEqual(decision.final_text, "")
        self.assertTrue(decision.blocks_publish)

    def test_a_rule_hit_on_the_transcript_promotes_a_clear_item(self):
        """원문은 모호한데 AI가 혼자 해소한 경우도 진행자에게 묻는다."""
        _, by_topic, _ = self._compiled({"topic": "QA 범위", "ambiguous": False,
                                         "final": "민수가 다음 주 금요일까지 QA를 끝낸다",
                                         "evidence": "QA는 다음 주쯤 할게요."})
        decision = by_topic["QA 범위"]
        self.assertEqual(decision.warnings, ["원문에 모호한 표현이 있어요: '쯤'"])
        self.assertEqual(decision.interpretations.count(), 1)
        self.assertTrue(decision.blocks_publish)

    def test_an_ambiguous_item_without_a_reason_still_needs_the_host(self):
        """AI가 모호하다고 하면서 이유를 비워 보내도 확인 대상으로 남아야 한다.

        이유와 규칙 사유가 모두 비면 has_warning이 False가 되어, AI가 갈린다고 본 항목이
        진행자 확인 없이 '그대로 공개될 결정'으로 분류된다.
        """
        item = {"topic": "로그 보관 기간", "ambiguous": True, "candidate": "민수가 30일로 바꾼다"}
        _, by_topic, _ = self._compiled(item)
        decision = by_topic["로그 보관 기간"]
        self.assertTrue(decision.divergence_reason)
        self.assertTrue(decision.has_warning)
        self.assertTrue(decision.blocks_publish)
        self.assertEqual(decision.interpretations.count(), 1)

    def test_compiling_twice_at_once_is_refused_not_crashed(self):
        """두 요청이 동시에 추출하면 사유를 보여준다 — 500이 아니라.

        이미 추출됐는지 보는 검사가 LLM 호출 앞에 있으므로, 버튼을 두 번 누르면 두 요청이
        모두 빈 상태를 보고 통과해 같은 order를 쓰려 한다.
        """
        def create_then_answer(*args, **kwargs):
            # 먼저 들어온 요청이 저장을 끝낸 상황을 흉내 낸다.
            Decision.objects.create(meeting=self.meeting, order=0, what="먼저 저장된 결정")
            return json.dumps([self.CLEAR], ensure_ascii=False)

        with patch.dict(os.environ, {"OLLAMA_MODEL": "m1"}), \
             patch.object(services, "_generate", side_effect=create_then_answer):
            with self.assertRaises(decision_compiler.DecisionCompileError):
                decision_compiler.compile_meeting(self.meeting)
        # 먼저 저장된 결정만 남고 반쯤 저장된 항목이 섞이지 않는다.
        self.assertEqual([d.what for d in self.meeting.decisions.all()], ["먼저 저장된 결정"])

    def test_the_rules_do_not_scan_candidates_of_an_ambiguous_item(self):
        item = dict(self.AMBIGUOUS, candidate="민수가 30일까지 50개로 바꾼다")
        _, by_topic, _ = self._compiled(item)
        decision = by_topic["로그 보관 기간"]
        self.assertEqual(decision.warnings, [])
        self.assertTrue(decision.divergence_reason)

    # ---- 근거 검증 ----

    def test_a_clear_item_without_usable_evidence_is_promoted(self):
        _, by_topic, _ = self._compiled(dict(self.CLEAR, evidence="회의에 없던 문장입니다."))
        decision = by_topic["배포 스크립트 정리"]
        self.assertEqual(decision.final_text, "")
        self.assertEqual(decision.interpretations.count(), 0)
        self.assertTrue(decision.has_warning)
        self.assertTrue(decision.blocks_publish)

    def test_an_ambiguous_item_keeps_its_sentence_even_with_fabricated_evidence(self):
        item = dict(self.AMBIGUOUS, evidence="회의에 없던 문장입니다.")
        summary, by_topic, _ = self._compiled(item)
        self.assertEqual(summary["ambiguous"], 1)
        self.assertEqual(summary["few_candidates"], 0)
        decision = by_topic["로그 보관 기간"]
        self.assertEqual(decision.interpretations.count(), 1)
        self.assertEqual(decision.evidence, "")

    def test_an_ambiguous_item_whose_only_sentence_is_vague_has_no_suggestion(self):
        summary, by_topic, _ = self._compiled(dict(self.AMBIGUOUS, candidate="민수가 적당히 바꾼다"))
        self.assertEqual(summary["few_candidates"], 1)
        self.assertEqual(by_topic["로그 보관 기간"].interpretations.count(), 0)

    def test_one_broken_item_does_not_take_the_others_down(self):
        broken = {"topic": "회의록 공유", "ambiguous": True, "why": "범위가 갈립니다", "candidate": {"엉뚱한": "형식"}}
        summary, by_topic, _ = self._compiled(self.CLEAR, self.AMBIGUOUS, broken)
        self.assertEqual(summary["decisions"], 3)
        self.assertEqual(summary["few_candidates"], 1)
        self.assertEqual(by_topic["회의록 공유"].interpretations.count(), 0)

    # ---- 상한 없음 ----

    def test_eight_ambiguous_items_all_wait_for_the_host(self):
        summary, _, _ = self._compiled(*[dict(self.AMBIGUOUS, topic="안건 %d" % i)
                                         for i in range(8)])
        self.assertEqual(summary["ambiguous"], 8)
        self.assertEqual(
            len(decision_compiler.publish_blockers(list(self.meeting.decisions.all()))), 8)

    # ---- 실패 처리와 가짜 응답 모드 ----

    def test_broken_json_is_retried_once_then_fails(self):
        with patch.dict(os.environ, {"OLLAMA_MODEL": "m1"}), self._llm("못 하겠습니다", "역시 못 하겠습니다") as gen:
            with self.assertRaises(decision_compiler.DecisionCompileError):
                decision_compiler.compile_meeting(self.meeting)
        self.assertEqual(gen.call_count, decision_compiler.PARSE_RETRIES + 1)
        # 실패했으면 반쯤 저장된 결정이 남아 있어서는 안 된다.
        self.assertEqual(self.meeting.decisions.count(), 0)

    def test_second_attempt_succeeding_is_accepted(self):
        good = json.dumps([self.CLEAR], ensure_ascii=False)
        with patch.dict(os.environ, {"OLLAMA_MODEL": "m1"}), self._llm("못 하겠습니다", good) as gen:
            decision_compiler.compile_meeting(self.meeting)
        self.assertEqual(gen.call_count, 2)
        self.assertEqual(self.meeting.decisions.count(), 1)

    def test_compile_fails_when_no_decision_can_be_extracted(self):
        with patch.dict(os.environ, {"OLLAMA_MODEL": "m1"}), self._llm("[]", "[]"):
            with self.assertRaises(decision_compiler.DecisionCompileError):
                decision_compiler.compile_meeting(self.meeting)

    def test_recompiling_an_already_compiled_meeting_is_refused(self):
        # 공개 뒤에 결정이 바뀌면 팀원이 확인한 내용과 달라진다.
        with patch.dict(os.environ, {}, clear=True):
            decision_compiler.compile_meeting(self.meeting)
            with self.assertRaises(decision_compiler.DecisionCompileError):
                decision_compiler.compile_meeting(self.meeting)

    def test_connection_failure_falls_back_to_the_fake_response(self):
        # 연결 실패는 실패 표시 대신 가짜 응답으로 흐름을 잇는다.
        with patch.dict(os.environ, {"OLLAMA_MODEL": "m1"}), patch.object(services, "_generate", side_effect=OSError("연결 실패")):
            summary = decision_compiler.compile_meeting(self.meeting)
        self.assertEqual(summary["decisions"], len(decision_compiler.FAKE_DECISIONS))

    def test_fake_mode_runs_the_whole_flow_without_any_llm(self):
        with patch.dict(os.environ, {}, clear=True):
            summary = decision_compiler.compile_meeting(self.meeting)
        self.assertEqual(summary["decisions"], 3)
        self.assertEqual(summary["ambiguous"], 2)
        self.assertEqual(summary["few_candidates"], 0)
        # 모호 항목마다 AI 추천 문장이 정확히 하나씩 붙는다.
        self.assertEqual(Interpretation.objects.filter(decision__meeting=self.meeting).count(), 2)
        self.assertEqual(
            Interpretation.objects.filter(decision__meeting=self.meeting, recommended=True).count(), 2)

    def test_fake_mode_decisions_carry_evidence(self):
        """가짜 모드도 '원문: …'이 보이도록 결정마다 원문 인용을 담는다 — 시연에서 가장 중요한 칸이다."""
        with patch.dict(os.environ, {}, clear=True):
            decision_compiler.compile_meeting(self.meeting)
        for decision in self.meeting.decisions.all():
            self.assertTrue(decision.evidence, decision.what)
        self.assertFalse(Interpretation.objects.filter(decision__meeting=self.meeting).exclude(evidence="").exists())

    def test_fake_mode_leaves_the_clear_item_ready_to_publish(self):
        with patch.dict(os.environ, {}, clear=True):
            decision_compiler.compile_meeting(self.meeting)
        clear = [d for d in self.meeting.decisions.all() if not d.has_warning]
        self.assertEqual(len(clear), 1)
        self.assertTrue(clear[0].final_text)
        self.assertEqual(clear[0].interpretations.count(), 0)
        for decision in self.meeting.decisions.all():
            if decision.has_warning:
                self.assertGreater(decision.interpretations.count(), 0, decision.what)

    # ---- 보류 신호(DEFERRAL_PHRASES) 자동 처리 ----

    def test_non_deferred_ambiguous_item_is_not_marked_undecided(self):
        _, by_topic, _ = self._compiled(self.AMBIGUOUS)
        self.assertFalse(by_topic["로그 보관 기간"].marked_undecided)

    def test_deferred_evidence_sets_undecided_by_default(self):
        """'내일 다시 얘기' 같은 근거가 있으면 AI가 뭐라고 했든 '다음 회의로 넘김'을 기본값으로 건다."""
        source = "A안과 B안을 놓고 일단 둘 다 준비해보면서 내일 다시 얘기해보기로 했다."
        self.meeting.transcript = source
        self.meeting.save(update_fields=["transcript"])
        item = {
            "topic": "디자인 선택", "ambiguous": True, "why": "A안/B안 중 무엇을 고를지 아직 안 정해짐",
            "evidence": source, "candidate": "A안으로 진행한다",
        }
        _, by_topic, _ = self._compiled(item)
        decision = by_topic["디자인 선택"]
        self.assertTrue(decision.marked_undecided)
        self.assertEqual(decision.final_text, decision_compiler.undecided_sentence("디자인 선택"))
        self.assertEqual(decision.published_text, decision.final_text)
        # 추천 문장은 그대로 남겨 둬서 진행자가 보고 다시 고를 수 있다.
        self.assertEqual(decision.interpretations.count(), 1)

    def test_deferred_evidence_in_an_old_style_response_is_still_caught(self):
        source = "A안과 B안을 놓고 일단 둘 다 준비해보면서 내일 다시 얘기해보기로 했다."
        self.meeting.transcript = source
        self.meeting.save(update_fields=["transcript"])
        item = {"topic": "디자인 선택", "ambiguous": True, "why": "x",
                "candidates": [{"text": "A안으로 진행한다", "evidence": source, "recommended": True}]}
        _, by_topic, _ = self._compiled(item)
        self.assertTrue(by_topic["디자인 선택"].marked_undecided)

    def test_clear_item_with_deferral_evidence_is_also_marked_undecided(self):
        source = "예산 건은 보류하고 다음에 다시 얘기하기로 했다."
        self.meeting.transcript = source
        self.meeting.save(update_fields=["transcript"])
        item = {"topic": "예산", "ambiguous": False, "final": "예산은 보류한다", "evidence": source}
        _, by_topic, _ = self._compiled(item)
        self.assertTrue(by_topic["예산"].marked_undecided)

    # ---- draft_hint: '직접 쓰기' 출발점 ----

    def test_ambiguous_item_carries_its_draft_hint_onto_the_decision(self):
        item = {"topic": "문서 전달", "ambiguous": True, "why": "x",
                "candidates": [{"text": "문서를 그쪽에 넘긴다", "evidence": "", "recommended": True}]}
        _, by_topic, _ = self._compiled(item)
        self.assertEqual(by_topic["문서 전달"].draft_hint, "문서를 (구체적으로 적기)에 넘긴다")

    def test_clear_item_whose_final_text_has_a_referent_gets_a_draft_hint(self):
        source = "담당자가 그쪽에 문서를 넘긴다."
        self.meeting.transcript = source
        self.meeting.save(update_fields=["transcript"])
        item = {"topic": "문서 전달", "ambiguous": False, "final": source, "evidence": source}
        _, by_topic, _ = self._compiled(item)
        self.assertEqual(by_topic["문서 전달"].draft_hint, "담당자가 (구체적으로 적기)에 문서를 넘긴다.")

    def test_clear_item_without_a_warning_has_no_draft_hint(self):
        _, by_topic, _ = self._compiled(self.CLEAR)
        self.assertEqual(by_topic["배포 스크립트 정리"].draft_hint, "")


class DecisionAmbiguityHintTests(TestCase):
    """_review_hints(정규식 탐지 → 프롬프트 힌트)와 _looks_deferred(보류 강제)는 DB 없이 동작하는 순수 함수다."""

    def test_review_hints_picks_sentences_with_known_phrases(self):
        source = "오늘은 날씨가 좋다. 그거 두 번째 걸로 그분한테 넘겨주세요. 예산은 대충 여유있게 잡아주세요."
        hints = decision_compiler._review_hints(source)
        self.assertEqual(len(hints), 2)
        self.assertTrue(any("그분" in h for h in hints))
        self.assertTrue(any("대충" in h for h in hints))

    def test_review_hints_is_empty_when_nothing_matches(self):
        self.assertEqual(decision_compiler._review_hints("오늘은 날씨가 좋다. 내일 또 보자."), [])

    def test_compile_prompt_includes_hint_sentences(self):
        prompt = decision_compiler._compile_prompt("예산은 대충 여유있게 잡아주세요.")
        self.assertIn("반드시 검토하라", prompt)
        self.assertIn("예산은 대충 여유있게 잡아주세요.", prompt)

    def test_compile_prompt_has_no_hint_block_when_nothing_matches(self):
        prompt = decision_compiler._compile_prompt("오늘 회의를 마칩니다.")
        self.assertNotIn("반드시 검토하라", prompt)

    def test_looks_deferred_true_for_ambiguous_item_evidence(self):
        item = {"ambiguous": True, "evidence": "일단 둘 다 준비해보고 내일 다시 얘기하자", "candidates": []}
        self.assertTrue(decision_compiler._looks_deferred(item))

    def test_looks_deferred_false_without_deferral_phrase(self):
        item = {"ambiguous": True, "evidence": "50개로 확정한다", "candidates": []}
        self.assertFalse(decision_compiler._looks_deferred(item))

    def test_looks_deferred_checks_evidence_for_clear_items_too(self):
        item = {"ambiguous": False, "evidence": "예산 건은 보류하기로 했다"}
        self.assertTrue(decision_compiler._looks_deferred(item))

    # ---- context_around: 근거 문장 앞뒤 맥락 ----

    def test_context_around_adds_the_sentence_before_and_after(self):
        source = "첫 문장이다. 근거가 되는 문장이다. 마지막 문장이다."
        context = decision_compiler.context_around(source, "근거가 되는 문장이다.")
        self.assertEqual(context, source)

    def test_context_around_matches_loosely(self):
        """공백 차이는 무시하고 찾는다(services._normalize_for_match와 같은 기준)."""
        source = "첫 문장이다. 근거가  되는\n문장이다. 마지막 문장이다."
        context = decision_compiler.context_around(source, "근거가 되는 문장이다.")
        self.assertIn("첫 문장이다.", context)
        self.assertIn("마지막 문장이다.", context)

    def test_context_around_is_empty_when_evidence_is_the_whole_source(self):
        """더할 앞뒤 문장이 없으면 근거와 똑같은 내용을 또 보여주지 않는다."""
        self.assertEqual(decision_compiler.context_around("문장 하나뿐이다.", "문장 하나뿐이다."), "")

    def test_context_around_is_empty_when_evidence_is_not_found(self):
        self.assertEqual(decision_compiler.context_around("원문입니다.", "회의에 없던 문장"), "")

    def test_context_around_is_empty_without_evidence_or_source(self):
        self.assertEqual(decision_compiler.context_around("원문입니다.", ""), "")
        self.assertEqual(decision_compiler.context_around("", "근거"), "")

    # ---- warning_context: 경고에 찍힌 표현이 원문 어디서 나왔는지 ----

    def test_warning_context_finds_the_quoted_phrase_in_the_transcript(self):
        """실제로 겪은 버그: 근거 문장엔 '그쪽'이 없는데 확정 문장엔 남아있던 경우."""
        source = "그거 정리해서 그쪽에 넘겨주실 수 있나요? 네, 제가 할게요. 목요일까지 넘기겠습니다."
        warning = "확정 문장에 모호한 표현이 있어요: '그쪽'"
        context = decision_compiler.warning_context(source, warning)
        self.assertIn("그쪽", context)
        self.assertIn("그거 정리해서", context)

    def test_warning_context_is_empty_for_a_quantity_conflict_warning(self):
        """숫자·날짜 충돌 경고는 인용 부호가 없다 — 값 자체가 이미 경고 문구에 다 있다."""
        warning = "확정 문장에 다른 숫자가 섞여 있어요: 30개, 50개"
        self.assertEqual(decision_compiler.warning_context("아무 원문", warning), "")

    def test_warning_context_is_empty_when_the_phrase_is_not_in_the_transcript(self):
        warning = "확정 문장에 모호한 표현이 있어요: '그쪽'"
        self.assertEqual(decision_compiler.warning_context("저쪽에 관한 이야기만 있는 원문입니다.", warning), "")


class DecisionReviewTests(TestCase):
    """진행자 화면의 권한과 동작."""

    def setUp(self):
        self.host = User.objects.create_user("host", password="pw12345!")
        self.member = User.objects.create_user("member", password="pw12345!")
        self.outsider = User.objects.create_user("outsider", password="pw12345!")
        self.team = Team.objects.create(name="t")
        self.team.members.add(self.host, self.member)
        self.meeting = Meeting.objects.create(
            team=self.team, title="m", host=self.host,
            processing_status=Meeting.STATUS_DONE,
            record="결정: 배포는 금요일",
            transcript="민수가 금요일까지 배포를 맡기로 했습니다.",
        )
        self.url = reverse("decision_review", args=[self.meeting.id])
        self.client.force_login(self.host)

    def _compile(self):
        with patch.dict(os.environ, {}, clear=True):
            self.client.post(self.url, {"action": "compile"})

    def test_outsider_gets_404(self):
        client = Client()
        client.force_login(self.outsider)
        self.assertEqual(client.get(self.url).status_code, 404)

    def test_non_host_member_gets_403(self):
        client = Client()
        client.force_login(self.member)
        self.assertEqual(client.get(self.url).status_code, 403)

    def test_anonymous_user_is_redirected_to_login(self):
        client = Client()
        self.assertEqual(client.get(self.url).status_code, 302)

    def test_meeting_without_a_finished_record_gets_404(self):
        Meeting.objects.filter(pk=self.meeting.pk).update(processing_status=Meeting.STATUS_PROCESSING)
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_first_visit_offers_the_compile_button_only(self):
        r = self.client.get(self.url)
        self.assertContains(r, "결정 사항 추출")
        self.assertNotContains(r, "투표 시작")
        self.assertFalse(r.context["compiled"])

    def test_compile_action_saves_specs_and_redirects(self):
        with patch.dict(os.environ, {}, clear=True):
            r = self.client.post(self.url, {"action": "compile"})
        self.assertRedirects(r, self.url)
        self.assertEqual(self.meeting.decisions.count(), len(decision_compiler.FAKE_DECISIONS))
        r = self.client.get(self.url)
        self.assertTrue(r.context["compiled"])
        # 가짜 응답 3건 중 2건이 확인 필요(모호)이고, 둘 다 아직 진행자가 고르지 않았다.
        self.assertEqual(r.context["blocker_count"], 2)

    def test_compile_failure_is_shown_on_the_page(self):
        with patch.dict(os.environ, {"OLLAMA_MODEL": "m1"}), patch.object(services, "_generate", return_value="안 되겠습니다"):
            r = self.client.post(self.url, {"action": "compile"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "읽지 못했습니다")
        self.assertEqual(self.meeting.decisions.count(), 0)

    def test_specs_cannot_be_edited_once_published(self):
        self._compile()
        decision = self.meeting.decisions.first()
        Decision.objects.filter(pk=decision.pk).update(status=Decision.STATUS_PUBLISHED)
        before = decision.who
        self.client.post(self.url, {
            "action": "save",
            "who_%d" % decision.id: "바뀐 담당자",
            "what_%d" % decision.id: decision.what,
        })
        decision.refresh_from_db()
        self.assertEqual(decision.who, before)
        self.assertFalse(self.client.get(self.url).context["editable"])

    def test_reset_is_refused_after_publishing(self):
        self._compile()
        self.meeting.decisions.update(status=Decision.STATUS_PUBLISHED)
        r = self.client.post(self.url, {"action": "reset"})
        self.assertContains(r, "지울 수 없습니다")
        self.assertGreater(self.meeting.decisions.count(), 0)

    def test_reset_clears_everything_while_still_draft(self):
        self._compile()
        self.client.post(self.url, {"action": "reset"})
        self.assertEqual(self.meeting.decisions.count(), 0)
        self.assertEqual(Interpretation.objects.count(), 0)

    def test_review_page_never_prints_member_names(self):
        self._compile()
        r = self.client.get(self.url)
        for name in ("member", "outsider"):
            self.assertNotContains(r, name)

    def test_the_screen_offers_choices_for_an_ai_only_ambiguous_item(self):
        """AI가 모호하다고 본 항목(규칙 사유는 없음)에도 선택지를 그려야 한다.

        그리지 않으면 진행자는 "확정하지 않은 항목이 있습니다"를 보면서 처리할 UI를 찾을 수
        없고 공개가 영구 차단된다.
        """
        self._compile()
        decision = next(d for d in self.meeting.decisions.all() if d.has_warning)
        self.assertEqual(decision.warnings, [])          # AI 판정만 있는 항목
        html = self.client.get(self.url).content.decode()
        self.assertIn('name="choice_%d"' % decision.id, html)
        # 사유에 따옴표가 들어 있어 템플릿이 이스케이프한다.
        from django.utils.html import escape

        self.assertIn(escape(decision.divergence_reason), html)

    def test_the_note_block_does_not_mention_a_removed_limit(self):
        html = self.client.get(self.url).content.decode()
        self.assertNotIn("회의당 최대 개", html)
        self.assertNotIn("빈칸", html)

    def test_the_header_counts_items_that_need_the_host(self):
        self._compile()
        r = self.client.get(self.url)
        # 가짜 응답 3건 중 2건이 확인 필요다. warnings만 세면 0개로 나와 공개 차단과 모순된다.
        self.assertEqual(r.context["ambiguous_count"], 2)

    def test_the_screen_splits_items_into_two_groups(self):
        """확인이 필요한 결정과 그대로 공개될 결정을 따로 묶어 보여준다."""
        self._compile()
        r = self.client.get(self.url)
        self.assertEqual(len(r.context["ambiguous_rows"]), 2)
        self.assertEqual(len(r.context["clear_rows"]), 1)
        for row in r.context["ambiguous_rows"]:
            self.assertTrue(row["decision"].has_warning)
        for row in r.context["clear_rows"]:
            self.assertFalse(row["decision"].has_warning)
            # 그대로 공개될 항목은 AI가 쓴 문장을 이미 갖고 있다.
            self.assertTrue(row["decision"].final_text)
        self.assertContains(r, "확인이 필요한 결정")
        self.assertContains(r, "그대로 공개될 결정")

    def test_the_screen_has_no_spec_input_fields_left(self):
        """누가·무엇을·언제까지·완료기준 입력 칸과 후보 편집 UI가 화면에서 사라졌다."""
        self._compile()
        html = self.client.get(self.url).content.decode()
        decision = self.meeting.decisions.first()
        for name in ("who_", "what_", "due_", "done_", "confirm_state_", "drop_interpretation_"):
            self.assertNotIn("%s%d" % (name, decision.id), html)
        self.assertNotIn('name="interpretation_', html)

    def test_the_recommended_candidate_is_not_preselected(self):
        """'AI 추천'은 배지일 뿐이다 — 진행자가 읽고 직접 탭해야 한다."""
        self._compile()
        r = self.client.get(self.url)
        self.assertContains(r, "AI 추천")
        for row in r.context["ambiguous_rows"]:
            for interpretation in row["interpretations"]:
                self.assertFalse(interpretation.chosen)
        # checked 속성이 붙은 input이 하나도 없어야 한다(스크립트의 :checked 선택자와 구별한다).
        self.assertEqual(re.findall(r"<input[^>]*\bchecked\b", r.content.decode()), [])

    def test_publishing_saves_the_choices_on_the_screen_first(self):
        """'확정 문장 공개'가 화면의 선택을 함께 저장한 뒤 공개한다 — 저장을 따로 누르지 않아도.

        저장과 공개가 두 단계로 나뉘어 있어서, 라디오를 다 고르고 공개를 누르면 "처리하지 않은
        항목이 있습니다"가 나오는 것이 진행자가 가장 자주 걸리던 지점이었다.
        """
        self._compile()
        fields = {"action": "publish"}
        for decision in self.meeting.decisions.all():
            if decision.has_warning:
                fields["choice_%d" % decision.id] = "%s%d" % (
                    decision_compiler.CHOICE_CANDIDATE_PREFIX,
                    decision.interpretations.first().id,
                )
        self.client.post(self.url, fields)
        for decision in self.meeting.decisions.all():
            self.assertEqual(decision.status, Decision.STATUS_PUBLISHED, decision.what)
            self.assertTrue(decision.final_text, decision.what)

    def test_publishing_with_a_bad_choice_keeps_what_was_saved(self):
        """선택이 부족하면 저장된 만큼은 남기고 사유를 보여준다."""
        self._compile()
        ambiguous = [d for d in self.meeting.decisions.all() if d.has_warning]
        first, second = ambiguous[0], ambiguous[1]
        r = self.client.post(self.url, {
            "action": "publish",
            "choice_%d" % first.id: "%s%d" % (
                decision_compiler.CHOICE_CANDIDATE_PREFIX, first.interpretations.first().id),
            "choice_%d" % second.id: decision_compiler.CHOICE_CUSTOM,
            "custom_text_%d" % second.id: "   ",
        })
        self.assertContains(r, "한 줄 적어 주세요")
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertTrue(first.final_text)                      # 저장된 만큼은 남는다
        self.assertEqual(second.final_text, "")
        self.assertEqual(first.status, Decision.STATUS_DRAFT)  # 공개는 안 됐다

    def test_each_draft_item_is_marked_for_the_button_script(self):
        """공개 버튼을 저장 전에도 갱신할 수 있도록, 선택을 바꿀 수 있는 항목마다 표시를 남긴다.

        마커는 draft 전체에 붙고, '미확정' 배지의 초기 노출만 blocks_publish를 따른다.
        """
        self._compile()
        html = self.client.get(self.url).content.decode()
        for decision in self.meeting.decisions.all():
            self.assertIn('data-choice-name="choice_%d"' % decision.id, html, decision.what)
            badge = 'data-unsettled-for="choice_%d"' % decision.id
            if decision.blocks_publish:
                self.assertIn(badge, html, decision.what)
        # 고르지 않은 항목이 있어도 공개 버튼은 눌린다 — 공개하면 그 항목이 빠진다.
        self.assertNotIn('id="publish-btn" disabled', html)

    def test_the_publish_confirmation_says_how_to_undo(self):
        """공개 후에도 되돌릴 길이 있다 — 확인창이 그렇게 말해야 한다.

        공개하면 참석자에게 알림이 가고, '공개 취소'로 draft로 내릴 수 있다. 고르지 않아 빠지는
        항목은 되돌릴 수 없으니 확인창이 그 수를 함께 말한다.
        """
        self._compile()
        html = self.client.get(self.url).content.decode()
        self.assertIn("참석자에게 알림이 가고", html)
        self.assertIn("'공개 취소'로 공개는 되돌릴 수 있어요", html)
        self.assertIn("고르지 않은", html)

    def test_the_screen_marks_rows_and_the_header_for_live_updates(self):
        """'미확정' 배지와 상단 '처리 안 한 항목' 수도 저장 전에 갱신할 수 있게 표시를 남긴다."""
        self._compile()
        html = self.client.get(self.url).content.decode()
        blocking = [d for d in self.meeting.decisions.all() if d.blocks_publish]
        self.assertEqual(len(blocking), 2)
        for decision in blocking:
            self.assertIn('data-unsettled-for="choice_%d"' % decision.id, html)
        self.assertIn('id="blocker-count"', html)
        self.assertIn('id="blocker-note"', html)

    def test_an_empty_published_sentence_can_still_be_fixed(self):
        """문장이 빈 공개 항목에도 수정 폼이 있어야 한다 — 없으면 손댈 방법이 전혀 없다."""
        self._compile()
        empty = Decision.objects.create(
            meeting=self.meeting, order=81, what="문장이 빈 공개 항목",
            status=Decision.STATUS_PUBLISHED, final_text="",
        )
        html = self.client.get(self.url).content.decode()
        self.assertIn('value="%d"' % empty.id, html)
        self.assertIn("문장이 비어 있어요", html)

    def test_the_revise_form_is_collapsed_under_the_published_sentence(self):
        """공개 뒤 '문장 고치기'는 확정 문장 바로 아래에서 찾을 수 있다."""
        self._compile()
        for decision in self.meeting.decisions.all():
            if decision.has_warning:
                self.client.post(self.url, {
                    "action": "save",
                    "choice_%d" % decision.id: "%s%d" % (
                        decision_compiler.CHOICE_CANDIDATE_PREFIX,
                        decision.interpretations.first().id),
                })
        self.client.post(self.url, {"action": "publish"})
        html = self.client.get(self.url).content.decode()
        self.assertIn("<summary", html)
        self.assertIn("문장 고치기", html)
        self.assertIn('name="final_text"', html)

    def test_numbering_runs_across_both_groups(self):
        self._compile()
        html = self.client.get(self.url).content.decode()
        # 확인 필요 2건 + 그대로 공개 1건이므로 번호는 1, 2, 3으로 이어진다.
        for number in ("1", "2", "3"):
            self.assertIn('<span class="item-icon">%s</span>' % number, html)

    def test_save_and_publish_live_in_the_fixed_button_bar(self):
        """저장·공개 버튼은 화면 아래 고정 줄 안에 있고 form 속성으로 폼에 묶인다(스크립트가 줄을 body로 옮긴다)."""
        self._compile()
        html = self.client.get(self.url).content.decode()
        bar = html[html.index('id="step-nav"'):html.index('id="blocker-help"')]
        self.assertIn('value="save" form="decision-form"', bar)
        self.assertIn('value="publish" form="decision-form"', bar)
        self.assertIn('id="decision-form"', html)

    def test_card_animation_does_not_leave_a_transform_behind(self):
        """fill-mode가 both면 끝난 뒤에도 transform이 남아 카드 안의 position:fixed가 카드 기준이 된다."""
        html = self.client.get(self.url).content.decode()
        self.assertNotIn("fadeInUp .5s cubic-bezier(.16,1,.3,1) both", html)

    def test_the_intro_line_describes_the_new_flow(self):
        self.assertContains(
            self.client.get(self.url),
            "회의에서 다르게 이해할 수 있는 결정을 AI가 찾아 추천 문장을 보여줍니다.",
        )

    def test_an_old_draft_without_a_sentence_is_shown_as_needing_the_host(self):
        """0022 이전에 만들어진 무경고 draft(문장 없음)는 '확인 필요'로 보이고, 고르지 않으면 공개 때 빠진다.

        화면의 판정과 서버의 처리가 어긋나면, 진행자는 "고르지 않은 항목 0개"를 보면서 항목이
        사라지는 이유를 찾을 수 없다.
        """
        self._compile()
        old = Decision.objects.create(
            meeting=self.meeting, order=80, what="이전 흐름에서 만든 항목",
            warnings=[], divergence_reason="", final_text="",
        )
        r = self.client.get(self.url)
        self.assertIn(old.id, [row["decision"].id for row in r.context["ambiguous_rows"]])
        self.assertTrue(next(row for row in r.context["rows"]
                             if row["decision"].id == old.id)["blocking"])
        self.assertGreaterEqual(r.context["blocker_count"], 1)
        self.assertTrue(r.context["can_publish"])

    def test_the_server_ignores_posted_spec_edits(self):
        """화면에서 칸을 없앴으니 서버도 그 값을 받지 않는다."""
        self._compile()
        decision = self.meeting.decisions.first()
        before = decision.what
        self.client.post(self.url, {
            "action": "save",
            "who_%d" % decision.id: "바뀐 담당자",
            "what_%d" % decision.id: "바뀐 안건",
            "due_%d" % decision.id: "바뀐 기한",
        })
        decision.refresh_from_db()
        self.assertEqual(decision.who, "")
        self.assertEqual(decision.what, before)
        self.assertEqual(decision.due_text, "")


class DecisionEntryLinkTests(TestCase):
    """회의록이 완성된 회의에서만 "결정 확인" 링크가 보인다. 진행자는 확정 화면, 팀원은 확인 화면."""

    def setUp(self):
        self.host = User.objects.create_user("host", password="pw12345!")
        self.member = User.objects.create_user("member", password="pw12345!")
        self.team = Team.objects.create(name="t")
        self.team.members.add(self.host, self.member)
        self.meeting = Meeting.objects.create(
            team=self.team, title="m", host=self.host,
            processing_status=Meeting.STATUS_DONE, record="결정", transcript="전사",
        )
        self.link = reverse("decision_review", args=[self.meeting.id])

    def test_result_page_shows_the_link_to_the_host(self):
        self.client.force_login(self.host)
        r = self.client.get(reverse("meeting_result", args=[self.meeting.id]))
        self.assertContains(r, "결정 확인")
        self.assertNotContains(r, "실험")
        self.assertContains(r, self.link)

    def test_result_page_hides_the_link_from_a_non_host_member(self):
        self.client.force_login(self.member)
        r = self.client.get(reverse("meeting_result", args=[self.meeting.id]))
        self.assertNotContains(r, self.link)

    def test_detail_page_hides_the_link_until_the_record_is_finished(self):
        self.client.force_login(self.host)
        Meeting.objects.filter(pk=self.meeting.pk).update(processing_status=Meeting.STATUS_PROCESSING)
        r = self.client.get(reverse("meeting_detail", args=[self.meeting.id]))
        self.assertNotContains(r, self.link)

    def test_detail_page_shows_the_link_when_the_record_is_finished(self):
        self.client.force_login(self.host)
        r = self.client.get(reverse("meeting_detail", args=[self.meeting.id]))
        self.assertContains(r, self.link)

    def test_member_sees_the_ack_link_instead_of_the_review_link(self):
        self.client.force_login(self.member)
        ack_link = reverse("decision_ack", args=[self.meeting.id])
        for name in ("meeting_result", "meeting_detail"):
            r = self.client.get(reverse(name, args=[self.meeting.id]))
            self.assertContains(r, ack_link, msg_prefix=name)
            self.assertNotContains(r, self.link, msg_prefix=name)

    def test_member_sees_no_ack_link_until_the_record_is_finished(self):
        self.client.force_login(self.member)
        Meeting.objects.filter(pk=self.meeting.pk).update(processing_status=Meeting.STATUS_PROCESSING)
        r = self.client.get(reverse("meeting_detail", args=[self.meeting.id]))
        self.assertNotContains(r, reverse("decision_ack", args=[self.meeting.id]))


class DecisionCarryoverTests(TestCase):
    """미결정 안건 이월. 진행자가 "처리됨"을 누를 때까지 이후 회의마다 계속 보인다."""

    def setUp(self):
        self.host = User.objects.create_user("hostuser", password="pw12345!")
        self.member = User.objects.create_user("memberuser", password="pw12345!")
        self.outsider = User.objects.create_user("outsideruser", password="pw12345!")
        self.team = Team.objects.create(name="t")
        self.team.members.add(self.host, self.member)
        self.first = Meeting.objects.create(
            team=self.team, title="첫 회의", host=self.host,
            processing_status=Meeting.STATUS_DONE, transcript="전사",
        )
        self.undecided = Decision.objects.create(
            meeting=self.first, order=0, who="민수", what="배포 범위 정하기",
            warnings=["완료 기준 빈칸"], status=Decision.STATUS_UNDECIDED,
        )
        self.second = Meeting.objects.create(team=self.team, title="두 번째 회의", host=self.host)
        self.detail_url = reverse("meeting_detail", args=[self.second.id])

    def _login(self, user):
        client = Client()
        client.force_login(user)
        return client

    def test_later_meeting_shows_the_undecided_item(self):
        r = self._login(self.host).get(self.detail_url)
        self.assertContains(r, "지난 회의 미결정 안건")
        self.assertContains(r, "배포 범위 정하기")
        self.assertContains(r, self.first.title)

    def test_the_originating_meeting_does_not_show_its_own_item(self):
        r = self._login(self.host).get(reverse("meeting_detail", args=[self.first.id]))
        self.assertNotContains(r, "지난 회의 미결정 안건")

    def test_published_and_draft_items_are_not_carried_over(self):
        for status in (Decision.STATUS_PUBLISHED, Decision.STATUS_DRAFT):
            Decision.objects.filter(pk=self.undecided.pk).update(status=status)
            r = self._login(self.host).get(self.detail_url)
            self.assertNotContains(r, "지난 회의 미결정 안건", msg_prefix=status)

    def test_members_see_the_item_but_no_resolve_button(self):
        resolve_url = reverse("decision_carryover_resolve", args=[self.second.id, self.undecided.id])
        r = self._login(self.member).get(self.detail_url)
        self.assertContains(r, "배포 범위 정하기")
        self.assertNotContains(r, resolve_url)
        r = self._login(self.host).get(self.detail_url)
        self.assertContains(r, resolve_url)

    def test_host_marking_it_handled_removes_it_from_later_meetings(self):
        url = reverse("decision_carryover_resolve", args=[self.second.id, self.undecided.id])
        r = self._login(self.host).post(url)
        self.assertRedirects(r, self.detail_url)
        self.undecided.refresh_from_db()
        self.assertIsNotNone(self.undecided.carryover_resolved_at)
        self.assertNotContains(self._login(self.host).get(self.detail_url), "지난 회의 미결정 안건")

    def test_it_keeps_showing_on_every_later_meeting_until_handled(self):
        third = Meeting.objects.create(team=self.team, title="세 번째 회의", host=self.host)
        host = self._login(self.host)
        self.assertContains(host.get(self.detail_url), "배포 범위 정하기")
        self.assertContains(host.get(reverse("meeting_detail", args=[third.id])), "배포 범위 정하기")
        host.post(reverse("decision_carryover_resolve", args=[third.id, self.undecided.id]))
        self.assertNotContains(host.get(self.detail_url), "배포 범위 정하기")
        self.assertNotContains(host.get(reverse("meeting_detail", args=[third.id])), "배포 범위 정하기")

    def test_non_host_cannot_mark_it_handled(self):
        url = reverse("decision_carryover_resolve", args=[self.second.id, self.undecided.id])
        self.assertEqual(self._login(self.member).post(url).status_code, 403)
        self.undecided.refresh_from_db()
        self.assertIsNone(self.undecided.carryover_resolved_at)

    def test_outsider_gets_404(self):
        url = reverse("decision_carryover_resolve", args=[self.second.id, self.undecided.id])
        self.assertEqual(self._login(self.outsider).post(url).status_code, 404)

    def test_another_teams_undecided_item_is_not_carried_over(self):
        other_team = Team.objects.create(name="other")
        other_team.members.add(self.host)
        other_meeting = Meeting.objects.create(
            team=other_team, title="남의 팀 회의", host=self.host, processing_status=Meeting.STATUS_DONE,
        )
        Decision.objects.create(
            meeting=other_meeting, order=0, what="남의 팀 미결정",
            warnings=["담당자 빈칸"], status=Decision.STATUS_UNDECIDED,
        )
        self.assertNotContains(self._login(self.host).get(self.detail_url), "남의 팀 미결정")

    def test_resolving_an_item_from_another_team_is_rejected(self):
        other_team = Team.objects.create(name="other")
        other_team.members.add(self.host)
        other_meeting = Meeting.objects.create(team=other_team, title="남의 팀", host=self.host)
        stranger = Decision.objects.create(
            meeting=other_meeting, order=0, what="남의 팀 미결정", status=Decision.STATUS_UNDECIDED,
        )
        url = reverse("decision_carryover_resolve", args=[self.second.id, stranger.id])
        self.assertEqual(self._login(self.host).post(url).status_code, 404)

    def test_carryover_banner_never_shows_who_voted(self):
        # 진행자 화면에는 참석자 칸이 있어 팀원 이름이 나온다 — 이월 안건 배너 안만 본다.
        html = self._login(self.host).get(self.detail_url).content.decode()
        start = html.index("지난 회의 미결정 안건")
        banner = html[start:html.index("</ul>", start)]
        self.assertIn("처리됨", banner)
        self.assertNotIn(self.member.username, banner)


class DecisionModelTests(TestCase):
    """결정 사항 추출 기능 모델 테스트.

    확인 기록이 (항목, 사용자)로 유일하면서 나중에 수정 가능하다는 것, 회의를 지우면
    결정 트리 전체가 함께 사라진다는 것을 고정한다.
    """

    def setUp(self):
        self.host = User.objects.create_user("host", password="pw12345!")
        self.member = User.objects.create_user("member", password="pw12345!")
        self.team = Team.objects.create(name="t")
        self.team.members.add(self.host, self.member)
        self.meeting = Meeting.objects.create(
            team=self.team, title="m", host=self.host,
            transcript="민수가 다음 주쯤 배포 준비를 맡기로 했다.",
        )

    def _decision(self, order=0, **kwargs):
        fields = {
            "who": "민수", "what": "배포 준비", "due_text": "다음 주쯤",
            "done_criteria": "", "confirm_state": Decision.CONFIRM_DISCUSSING,
            "warnings": ["모호어 '쯤'", "완료 기준 빈칸"],
        }
        fields.update(kwargs)
        return Decision.objects.create(meeting=self.meeting, order=order, **fields)

    # ---- Decision ----

    def test_decision_defaults(self):
        decision = self._decision()
        self.assertEqual(decision.status, Decision.STATUS_DRAFT)
        self.assertEqual(decision.final_text, "")
        self.assertFalse(decision.marked_undecided)
        self.assertFalse(decision.revised)
        # 미결정으로 이월된 안건만 carryover_resolved_at을 쓰므로 기본값은 비어 있어야 한다.
        self.assertIsNone(decision.carryover_resolved_at)
        self.assertTrue(decision.has_warning)

    def test_status_choices_are_only_the_three_we_kept(self):
        self.assertEqual(
            [value for value, _ in Decision.STATUS_CHOICES],
            [Decision.STATUS_DRAFT, Decision.STATUS_PUBLISHED, Decision.STATUS_UNDECIDED],
        )

    def test_warnings_json_round_trips_as_a_list(self):
        decision = self._decision()
        reloaded = Decision.objects.get(pk=decision.pk)
        self.assertEqual(reloaded.warnings, ["모호어 '쯤'", "완료 기준 빈칸"])
        empty = self._decision(order=1, warnings=[])
        self.assertEqual(Decision.objects.get(pk=empty.pk).warnings, [])
        self.assertFalse(empty.has_warning)

    def test_publishing_is_blocked_until_the_item_has_a_sentence(self):
        """공개를 막는 조건은 하나다 — 공개할 문장이 있는가.

        '다음 회의로 넘김'도 문장을 받으므로(undecided_sentence) 여기서 갈라지지 않는다.
        """
        decision = self._decision()
        self.assertTrue(decision.blocks_publish)
        decision.final_text = "금요일까지 스테이징 배포"
        self.assertFalse(decision.blocks_publish)

    def test_an_ai_only_reason_still_needs_the_host(self):
        """AI가 "다르게 이해할 수 있다"고 본 항목은 규칙 사유가 없어도 확인 대상이다."""
        ai_only = self._decision(
            order=5, warnings=[], divergence_reason="'충분히'가 30일인지 90일인지 갈립니다",
        )
        self.assertTrue(ai_only.has_warning)
        self.assertTrue(ai_only.needs_host)
        self.assertTrue(ai_only.blocks_publish)
        ai_only.final_text = "민수가 12월 5일까지 30일로 바꾼다"
        self.assertFalse(ai_only.blocks_publish)
        # 문장을 골랐어도 '확인이 필요했던 항목'이라는 사실은 그대로다(묶음이 바뀌지 않는다).
        self.assertTrue(ai_only.needs_host)

    def _migration(self):
        from importlib import import_module

        return import_module("meetalign.migrations.0022_decision_candidates")

    def test_0022_builds_a_sentence_from_the_old_fields(self):
        spec_sentence = self._migration().spec_sentence
        self.assertEqual(spec_sentence("민수", "배포", "금요일", "확인"), "민수 · 배포 · 금요일 · 확인")
        self.assertEqual(spec_sentence("", "배포", "", ""), "배포")
        self.assertEqual(spec_sentence("", "", "", ""), "")

    def test_0022_backfills_only_published_rows_without_a_sentence(self):
        """폴백을 지우면 문장이 비는 레코드만 채운다 — draft와 이미 쓰인 문장은 건드리지 않는다."""
        fill = self._migration().fill_missing_final_text
        draft = self._decision(order=11, who="민수", what="배포", due_text="금요일", done_criteria="확인")
        empty = self._decision(
            order=12, who="민수", what="문서", due_text="", done_criteria="",
            status=Decision.STATUS_PUBLISHED,
        )
        kept = self._decision(
            order=13, who="", what="QA", due_text="", done_criteria="",
            status=Decision.STATUS_UNDECIDED, final_text="이미 있는 문장",
        )
        self.assertEqual(fill(Decision), 1)
        draft.refresh_from_db()
        empty.refresh_from_db()
        kept.refresh_from_db()
        self.assertEqual(draft.final_text, "")
        self.assertEqual(empty.final_text, "민수 · 문서")
        self.assertEqual(kept.final_text, "이미 있는 문장")

    def test_0022_marks_backfilled_undecided_rows_as_carried_over(self):
        """보정된 미결정 행은 다음 회의로 넘어간 안건임이 문장에 드러나야 한다.

        팀원 화면이 상태 분기를 없애고 final_text만 보여주므로, 꼬리말이 없으면 미결정이
        보통 확정 결정과 똑같이 읽힌다.
        """
        fill = self._migration().fill_missing_final_text
        carried = self._decision(
            order=14, who="민수", what="배포 범위 정하기", due_text="", done_criteria="",
            status=Decision.STATUS_UNDECIDED,
        )
        self.assertEqual(fill(Decision), 1)
        carried.refresh_from_db()
        self.assertEqual(
            carried.final_text,
            "민수 · 배포 범위 정하기 — 이번 회의에서 결론이 나지 않았음 (다음 회의로)",
        )

    def test_items_without_warnings_do_not_need_the_host(self):
        decision = self._decision(warnings=[], final_text="민수가 금요일까지 배포한다")
        self.assertFalse(decision.needs_host)
        self.assertFalse(decision.blocks_publish)

    def test_duplicate_decision_order_in_one_meeting_is_rejected_by_db(self):
        self._decision(order=0)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._decision(order=0, who="다른 사람")

    def test_same_order_is_allowed_in_a_different_meeting(self):
        other = Meeting.objects.create(team=self.team, title="m2", host=self.host)
        self._decision(order=0)
        Decision.objects.create(meeting=other, order=0, who="민수", what="배포 준비")
        self.assertEqual(Decision.objects.filter(order=0).count(), 2)

    # ---- Interpretation ----

    def test_interpretation_labels_follow_order(self):
        decision = self._decision()
        labels = [
            Interpretation.objects.create(decision=decision, order=i, text="후보 %d" % i, evidence="민수가 다음 주쯤").label
            for i in range(3)
        ]
        self.assertEqual(labels, ["A", "B", "C"])

    def test_duplicate_interpretation_order_is_rejected_by_db(self):
        decision = self._decision()
        Interpretation.objects.create(decision=decision, order=0, text="후보 A")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Interpretation.objects.create(decision=decision, order=0, text="후보 B")

    # ---- DecisionAgreement (확인 / 달랐어요) ----

    def test_agreement_defaults_to_ack_without_a_comment(self):
        decision = self._decision(status=Decision.STATUS_PUBLISHED, final_text="금요일까지")
        agreement = DecisionAgreement.objects.create(decision=decision, user=self.member)
        self.assertEqual(agreement.kind, DecisionAgreement.KIND_ACK)
        self.assertEqual(agreement.comment, "")
        self.assertFalse(agreement.differs)

    def test_differ_can_carry_a_one_line_comment(self):
        decision = self._decision(status=Decision.STATUS_PUBLISHED, final_text="금요일까지")
        agreement = DecisionAgreement.objects.create(
            decision=decision, user=self.member,
            kind=DecisionAgreement.KIND_DIFFER, comment="저는 다음 주 금요일로 들었습니다",
        )
        self.assertTrue(agreement.differs)
        self.assertEqual(agreement.comment, "저는 다음 주 금요일로 들었습니다")

    def test_one_agreement_per_user_per_decision(self):
        decision = self._decision(status=Decision.STATUS_PUBLISHED, final_text="금요일까지")
        DecisionAgreement.objects.create(decision=decision, user=self.member)
        with self.assertRaises(IntegrityError), transaction.atomic():
            DecisionAgreement.objects.create(
                decision=decision, user=self.member, kind=DecisionAgreement.KIND_DIFFER,
            )

    def test_an_agreement_can_be_changed_after_it_is_saved(self):
        # 블라인드가 없어졌으므로 선택과 의견을 모두 바꿀 수 있다 — 집계는 항상 현재 선택 기준이다.
        decision = self._decision(status=Decision.STATUS_PUBLISHED, final_text="금요일까지")
        DecisionAgreement.objects.update_or_create(
            decision=decision, user=self.member, defaults={"kind": DecisionAgreement.KIND_ACK, "comment": ""},
        )
        DecisionAgreement.objects.update_or_create(
            decision=decision, user=self.member,
            defaults={"kind": DecisionAgreement.KIND_DIFFER, "comment": "다시 보니 달랐어요"},
        )
        agreement = DecisionAgreement.objects.get(decision=decision, user=self.member)
        self.assertEqual(DecisionAgreement.objects.filter(decision=decision).count(), 1)
        self.assertTrue(agreement.differs)
        self.assertEqual(agreement.comment, "다시 보니 달랐어요")

    def test_different_users_keep_separate_agreements(self):
        decision = self._decision(status=Decision.STATUS_PUBLISHED, final_text="금요일까지")
        DecisionAgreement.objects.create(decision=decision, user=self.host)
        DecisionAgreement.objects.create(
            decision=decision, user=self.member, kind=DecisionAgreement.KIND_DIFFER,
        )
        self.assertEqual(decision.agreements.count(), 2)

    # ---- Checkpoint ----

    def test_one_checkpoint_per_user_per_decision(self):
        decision = self._decision(status=Decision.STATUS_PUBLISHED, final_text="금요일까지 스테이징 배포")
        checkpoint = Checkpoint.objects.create(decision=decision, user=self.member, expected_text="배포 문서 작성")
        self.assertEqual(checkpoint.verdict, Checkpoint.VERDICT_UNKNOWN)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Checkpoint.objects.create(decision=decision, user=self.member, expected_text="다른 내용")

    # ---- 분리 확인 ----

    def test_deleting_the_meeting_removes_the_whole_decision_tree(self):
        decision = self._decision()
        Interpretation.objects.create(decision=decision, order=0, text="후보 A")
        DecisionAgreement.objects.create(decision=decision, user=self.member)
        Checkpoint.objects.create(decision=decision, user=self.member, expected_text="배포 문서 작성")
        self.meeting.delete()
        self.assertEqual(Decision.objects.count(), 0)
        self.assertEqual(Interpretation.objects.count(), 0)
        self.assertEqual(DecisionAgreement.objects.count(), 0)
        self.assertEqual(Checkpoint.objects.count(), 0)

    def test_quiz_models_and_tables_are_gone(self):
        from django.apps import apps
        for name in ("CheckQuestion", "Answer"):
            with self.assertRaises(LookupError, msg=name):
                apps.get_model("meetalign", name)
        names = connection.introspection.table_names()
        self.assertNotIn("meetalign_checkquestion", names)
        self.assertNotIn("meetalign_answer", names)

    def test_meeting_has_no_quiz_transcript_field_and_still_saves(self):
        # tasks.refresh_meeting이 쓰는 update_fields 조합이 필드 삭제 후에도 FieldError 없이 돌아야 한다.
        self.assertNotIn("check_questions_transcript", [f.name for f in Meeting._meta.get_fields()])
        self.meeting.transcript = "전사"
        self.meeting.record = "회의록"
        self.meeting.processing_status = Meeting.STATUS_DONE
        self.meeting.save(update_fields=["transcript", "record", "processing_status"])
        self.meeting.refresh_from_db()
        self.assertEqual(self.meeting.record, "회의록")

    def test_the_blind_vote_model_is_gone(self):
        # 투표 모드는 보류했다. 되살릴 때는 새 마이그레이션으로 DecisionVote를 추가한다
        # (models.py 상단 메모 참고). 지금은 모델도 테이블도 없어야 한다.
        from django.apps import apps
        with self.assertRaises(LookupError):
            apps.get_model("meetalign", "DecisionVote")
        self.assertNotIn("meetalign_decisionvote", connection.introspection.table_names())


class DecisionConfirmTests(TestCase):
    """③ 진행자 확정 — 경고 항목마다 [후보 선택 | 직접 작성 | 아직 미정]."""

    def setUp(self):
        self.host = User.objects.create_user("hostuser", password="pw12345!")
        self.member = User.objects.create_user("memberuser", password="pw12345!")
        self.outsider = User.objects.create_user("outsideruser", password="pw12345!")
        self.team = Team.objects.create(name="t")
        self.team.members.add(self.host, self.member)
        self.meeting = Meeting.objects.create(
            team=self.team, title="m", host=self.host,
            processing_status=Meeting.STATUS_DONE,
            record="결정", transcript="민수가 금요일까지 배포를 맡기로 했습니다.",
        )
        self.url = reverse("decision_review", args=[self.meeting.id])
        self.client.force_login(self.host)
        with patch.dict(os.environ, {}, clear=True):
            self.client.post(self.url, {"action": "compile"})
        decisions = list(self.meeting.decisions.all())
        # 모호 판정은 AI가 하므로 divergence_reason만 있고 warnings는 빈 항목이 정상이다.
        self.warned = [d for d in decisions if d.has_warning]
        self.plain = [d for d in decisions if not d.has_warning]
        # 가짜 결정 사항 3개 중 2개에 경고가 붙고 1개는 완전히 채워져 있다.
        self.assertEqual(len(self.warned), 2)
        self.assertEqual(len(self.plain), 1)

    def _candidate_value(self, interpretation):
        return "%s%d" % (decision_compiler.CHOICE_CANDIDATE_PREFIX, interpretation.id)

    def _confirm_all(self, extra=None):
        """경고 항목 전부를 첫 후보로 확정한다."""
        fields = {"action": "save"}
        for decision in self.warned:
            fields["choice_%d" % decision.id] = self._candidate_value(decision.interpretations.first())
        fields.update(extra or {})
        return self.client.post(self.url, fields)

    # ---- 후보 선택 ----

    def test_choosing_a_candidate_copies_its_text_into_final_text(self):
        decision = self.warned[0]
        candidate = decision.interpretations.get()
        self.client.post(self.url, {
            "action": "save", "choice_%d" % decision.id: self._candidate_value(candidate),
        })
        decision.refresh_from_db()
        self.assertEqual(decision.final_text, candidate.text)
        self.assertFalse(decision.marked_undecided)
        self.assertFalse(decision.blocks_publish)

    def test_each_warned_item_offers_exactly_one_ai_sentence(self):
        html = self.client.get(self.url).content.decode()
        for decision in self.warned:
            self.assertEqual(decision.interpretations.count(), 1, decision.what)
            radios = re.findall(r'name="choice_%d" value="%s\d+"' % (decision.id, decision_compiler.CHOICE_CANDIDATE_PREFIX), html)
            self.assertEqual(len(radios), 1, decision.what)
        # 생성 문장에는 근거를 붙이지 않는다 — 후보마다 '근거:' 줄이 없다.
        self.assertNotIn("근거: &quot;", html)

    def test_the_chosen_text_is_a_snapshot_not_a_live_link(self):
        """고른 시점의 문장을 복사해 둔다 — 후보가 나중에 바뀌어도 확정 내용은 그대로다."""
        decision = self.warned[0]
        candidate = decision.interpretations.first()
        self.client.post(self.url, {
            "action": "save", "choice_%d" % decision.id: self._candidate_value(candidate),
        })
        chosen_text = candidate.text
        Interpretation.objects.filter(pk=candidate.pk).update(text="완전히 다른 후보 문장")
        decision.refresh_from_db()
        candidate.refresh_from_db()
        self.assertEqual(candidate.text, "완전히 다른 후보 문장")
        self.assertEqual(decision.final_text, chosen_text)

    def test_the_review_screen_shows_which_option_is_chosen(self):
        decision = self.warned[0]
        candidate = decision.interpretations.get()
        self.client.post(self.url, {
            "action": "save", "choice_%d" % decision.id: self._candidate_value(candidate),
        })
        row = next(r for r in self.client.get(self.url).context["rows"] if r["decision"].id == decision.id)
        self.assertEqual(row["choice"], self._candidate_value(candidate))
        self.assertFalse(row["blocking"])

    def test_a_candidate_belonging_to_another_decision_is_rejected(self):
        stranger = self.warned[1].interpretations.first()
        r = self.client.post(self.url, {
            "action": "save", "choice_%d" % self.warned[0].id: self._candidate_value(stranger),
        })
        self.assertEqual(r.status_code, 404)
        self.warned[0].refresh_from_db()
        self.assertEqual(self.warned[0].final_text, "")

    # ---- 직접 작성 ----

    def test_custom_choice_stores_the_stripped_text(self):
        decision = self.warned[0]
        self.client.post(self.url, {
            "action": "save",
            "choice_%d" % decision.id: decision_compiler.CHOICE_CUSTOM,
            "custom_text_%d" % decision.id: "  금요일 18시까지 스테이징 배포  ",
        })
        decision.refresh_from_db()
        self.assertEqual(decision.final_text, "금요일 18시까지 스테이징 배포")
        self.assertFalse(decision.marked_undecided)

    def test_custom_choice_without_text_is_refused(self):
        decision = self.warned[0]
        r = self.client.post(self.url, {
            "action": "save",
            "choice_%d" % decision.id: decision_compiler.CHOICE_CUSTOM,
            "custom_text_%d" % decision.id: "   ",
        })
        self.assertContains(r, "한 줄 적어 주세요")
        decision.refresh_from_db()
        self.assertEqual(decision.final_text, "")
        self.assertTrue(decision.blocks_publish)

    def test_custom_text_is_shown_back_in_its_own_box(self):
        decision = self.warned[0]
        self.client.post(self.url, {
            "action": "save",
            "choice_%d" % decision.id: decision_compiler.CHOICE_CUSTOM,
            "custom_text_%d" % decision.id: "내가 쓴 확정 문장",
        })
        row = next(r for r in self.client.get(self.url).context["rows"] if r["decision"].id == decision.id)
        self.assertEqual(row["choice"], decision_compiler.CHOICE_CUSTOM)
        self.assertEqual(row["custom_text"], "내가 쓴 확정 문장")

    # ---- 다음 회의로 넘김 ----

    def test_undecided_sentence_names_the_topic(self):
        """문구는 한 곳에서만 만든다 — 진행자 화면·팀원 화면·이월 목록이 같은 문장을 쓴다."""
        self.assertEqual(
            decision_compiler.undecided_sentence("로그 보관 기간"),
            "로그 보관 기간 — 이번 회의에서 결론이 나지 않았음 (다음 회의로)",
        )

    def test_marking_undecided_sets_the_flag_and_writes_a_sentence(self):
        """'다음 회의로 넘김'도 자기 문장을 갖는다 — 공개되는 것은 언제나 final_text 하나다."""
        decision = self.warned[0]
        self.client.post(self.url, {
            "action": "save",
            "choice_%d" % decision.id: decision_compiler.CHOICE_CUSTOM,
            "custom_text_%d" % decision.id: "먼저 쓴 문장",
        })
        self.client.post(self.url, {
            "action": "save", "choice_%d" % decision.id: decision_compiler.CHOICE_UNDECIDED,
        })
        decision.refresh_from_db()
        self.assertTrue(decision.marked_undecided)
        self.assertEqual(
            decision.final_text,
            "%s — 이번 회의에서 결론이 나지 않았음 (다음 회의로)" % decision.what,
        )
        self.assertEqual(decision.published_text, decision.final_text)
        self.assertFalse(decision.blocks_publish)

    def test_switching_from_undecided_back_to_a_candidate_clears_the_flag(self):
        decision = self.warned[0]
        self.client.post(self.url, {
            "action": "save", "choice_%d" % decision.id: decision_compiler.CHOICE_UNDECIDED,
        })
        candidate = decision.interpretations.first()
        self.client.post(self.url, {
            "action": "save", "choice_%d" % decision.id: self._candidate_value(candidate),
        })
        decision.refresh_from_db()
        self.assertFalse(decision.marked_undecided)
        self.assertEqual(decision.final_text, candidate.text)

    # ---- 저장 규칙 ----

    def test_items_with_no_choice_posted_keep_their_confirmation(self):
        # 한 항목을 고치려고 저장했다가 다른 항목의 확정이 날아가면 안 된다.
        first, second = self.warned
        self.client.post(self.url, {
            "action": "save", "choice_%d" % first.id: self._candidate_value(first.interpretations.first()),
        })
        self.client.post(self.url, {
            "action": "save", "choice_%d" % second.id: decision_compiler.CHOICE_UNDECIDED,
        })
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.final_text, first.interpretations.first().text)
        self.assertTrue(second.marked_undecided)

    def test_an_unknown_choice_value_is_refused(self):
        decision = self.warned[0]
        r = self.client.post(self.url, {"action": "save", "choice_%d" % decision.id: "엉뚱한 값"})
        self.assertContains(r, "하나 골라 주세요")
        decision.refresh_from_db()
        self.assertEqual(decision.final_text, "")

    # ---- 이 결정 빼기 ----

    def test_a_clear_item_can_be_rewritten_by_hand(self):
        """'그대로 공개될 결정'도 접힌 '고치기'로 문장을 바꿀 수 있다."""
        decision = self.plain[0]
        self.client.post(self.url, {
            "action": "save",
            "choice_%d" % decision.id: decision_compiler.CHOICE_CUSTOM,
            "custom_text_%d" % decision.id: "민수가 금요일 18시까지 끝낸다",
        })
        decision.refresh_from_db()
        self.assertEqual(decision.final_text, "민수가 금요일 18시까지 끝낸다")

    def test_dropping_an_item_removes_it_from_the_publish_set(self):
        """AI가 회의에 없던 결정을 만들어 냈을 때 쓰는 통로 — 두 묶음 모두에서 된다."""
        for decision in (self.plain[0], self.warned[0]):
            self.client.post(self.url, {
                "action": "save", "choice_%d" % decision.id: decision_compiler.CHOICE_DROP,
            })
            self.assertFalse(Decision.objects.filter(pk=decision.pk).exists(), decision.what)
            self.assertEqual(Interpretation.objects.filter(decision_id=decision.pk).count(), 0)
        r = self.client.get(self.url)
        self.assertEqual(r.context["draft_count"], self.meeting.decisions.count())

    def test_dropping_is_refused_after_publishing(self):
        """공개된 뒤에는 뺄 수 없다 — 팀원 확인 기록이 딸려 있다."""
        decision = self.plain[0]
        Decision.objects.filter(pk=decision.pk).update(status=Decision.STATUS_PUBLISHED)
        r = self.client.post(self.url, {
            "action": "save", "choice_%d" % decision.id: decision_compiler.CHOICE_DROP,
        })
        self.assertContains(r, "공개된 항목은 뺄 수 없습니다")
        self.assertTrue(Decision.objects.filter(pk=decision.pk).exists())

    def test_a_failed_choice_holds_back_the_irreversible_drop(self):
        """제출이 실패하면 '이 결정 빼기'는 보류한다 — 되돌릴 수 없는 동작이기 때문이다.

        같은 제출이 거부됐는데 삭제만 커밋되면 진행자는 복구할 방법이 없다(다시 추출하면
        다른 선택도 전부 사라진다). 반면 나머지 선택은 그대로 저장해, 고른 것이 화면에서
        사라지지 않게 한다.
        """
        keep = self.plain[0]
        broken = self.warned[0]
        r = self.client.post(self.url, {
            "action": "publish",
            "choice_%d" % keep.id: decision_compiler.CHOICE_DROP,
            "choice_%d" % broken.id: decision_compiler.CHOICE_CUSTOM,
            "custom_text_%d" % broken.id: "   ",
        })
        self.assertContains(r, "한 줄 적어 주세요")
        self.assertTrue(Decision.objects.filter(pk=keep.pk).exists())
        self.assertEqual(self.meeting.decisions.filter(status=Decision.STATUS_PUBLISHED).count(), 0)

    def test_the_screen_asks_before_dropping_a_decision(self):
        """빼기는 비가역이므로 제출 전에 확인을 받는다(다른 파괴적 동작과 같은 수준)."""
        html = self.client.get(self.url).content.decode()
        self.assertIn('data-drop-value="%s"' % decision_compiler.CHOICE_DROP, html)
        self.assertIn("되돌릴 수 없어요", html)

    def test_a_carried_over_item_is_not_shown_as_publishing(self):
        """'다음 회의로 넘김'을 고른 항목을 '그대로 공개될 결정'으로 보여주면 안 된다.

        그 항목은 공개되지 않고 다음 회의로 이월된다 — 화면과 서버가 다른 말을 하면 진행자는
        자기가 고른 것이 반영됐는지 알 수 없다.
        """
        decision = self.plain[0]
        self.client.post(self.url, {
            "action": "save", "choice_%d" % decision.id: decision_compiler.CHOICE_UNDECIDED,
        })
        r = self.client.get(self.url)
        ids = lambda rows: [row["decision"].id for row in rows]
        self.assertNotIn(decision.id, ids(r.context["clear_rows"]))
        self.assertIn(decision.id, ids(r.context["carried_rows"]))
        self.assertContains(r, "다음 회의로 넘길 결정")
        html = r.content.decode()
        self.assertNotIn("기본값으로 그대로 공개돼요", html)

    def test_every_draft_row_is_tracked_by_the_button_script(self):
        """선택을 바꿀 수 있는 모든 항목을 스크립트가 추적해야 한다.

        처리한 항목만 빠지면, 그 항목을 빈 '직접 쓰기'로 바꿔도 배지와 개수가 반응하지 않는다.
        """
        html = self.client.get(self.url).content.decode()
        for decision in self.meeting.decisions.all():
            self.assertIn('data-choice-name="choice_%d"' % decision.id, html, decision.what)
        # 빈 '직접 쓰기'를 처리됨으로 세지 않도록 스크립트가 본문까지 본다.
        self.assertIn("custom_text_", html)

    def test_dropping_every_item_leaves_nothing_to_publish(self):
        for decision in list(self.meeting.decisions.all()):
            self.client.post(self.url, {
                "action": "save", "choice_%d" % decision.id: decision_compiler.CHOICE_DROP,
            })
        self.assertEqual(self.meeting.decisions.count(), 0)
        # 회의에 결정이 하나도 남지 않았으면 "공개할 항목이 없다"가 아니라 그 사실을 말해야 한다.
        self.assertContains(self.client.post(self.url, {"action": "publish"}),
                            "모든 결정을 뺐습니다")

    def test_items_without_warnings_are_ready_with_no_choice(self):
        plain = self.plain[0]
        self.assertFalse(plain.blocks_publish)
        r = self.client.get(self.url)
        row = next(r for r in r.context["rows"] if r["decision"].id == plain.id)
        self.assertFalse(row["blocking"])
        # '직접 쓰기' 칸이 접히지 않고 보이며, AI가 쓴 문장이 미리 들어 있다.
        self.assertContains(r, "이 칸의 문장이 그대로 공개돼요")
        self.assertContains(r, 'name="custom_text_%d" value="%s"' % (plain.id, plain.final_text))

    def test_editing_the_box_of_a_clear_item_changes_what_is_published(self):
        """그대로 공개 항목은 라디오를 누르지 않아도 '직접 쓰기' 칸의 문장으로 공개된다."""
        plain = self.plain[0]
        self.client.post(self.url, {"action": "save", "custom_text_%d" % plain.id: "  고친 문장으로 공개한다 "})
        plain.refresh_from_db()
        self.assertEqual(plain.final_text, "고친 문장으로 공개한다")
        self.assertFalse(plain.has_warning)

    def test_an_emptied_box_keeps_the_ai_sentence(self):
        plain = self.plain[0]
        original = plain.final_text
        self.client.post(self.url, {"action": "save", "custom_text_%d" % plain.id: "   "})
        plain.refresh_from_db()
        self.assertEqual(plain.final_text, original)

    def test_a_clear_item_can_still_be_carried_or_dropped(self):
        plain = self.plain[0]
        self.client.post(self.url, {
            "action": "save", "choice_%d" % plain.id: decision_compiler.CHOICE_UNDECIDED,
            "custom_text_%d" % plain.id: "무시된다",
        })
        plain.refresh_from_db()
        self.assertTrue(plain.marked_undecided)

    def test_non_host_cannot_confirm(self):
        client = Client()
        client.force_login(self.member)
        self.assertEqual(client.post(self.url, {"action": "save"}).status_code, 403)

    def test_outsider_gets_404(self):
        client = Client()
        client.force_login(self.outsider)
        self.assertEqual(client.post(self.url, {"action": "save"}).status_code, 404)


class DecisionPublishTests(TestCase):
    """④ 확정 문장 공개 — 모든 항목을 고르지 않아도 공개할 수 있고, 고르지 않은 확인 필요 항목은 빠진다."""

    def setUp(self):
        self.host = User.objects.create_user("hostuser", password="pw12345!")
        self.member = User.objects.create_user("memberuser", password="pw12345!")
        self.outsider = User.objects.create_user("outsideruser", password="pw12345!")
        self.team = Team.objects.create(name="t")
        self.team.members.add(self.host, self.member)
        self.meeting = Meeting.objects.create(
            team=self.team, title="m", host=self.host,
            processing_status=Meeting.STATUS_DONE,
            record="결정", transcript="민수가 금요일까지 배포를 맡기로 했습니다.",
        )
        self.url = reverse("decision_review", args=[self.meeting.id])
        self.client.force_login(self.host)
        with patch.dict(os.environ, {}, clear=True):
            self.client.post(self.url, {"action": "compile"})
        decisions = list(self.meeting.decisions.all())
        # 모호 판정은 AI가 하므로 divergence_reason만 있고 warnings는 빈 항목이 정상이다.
        self.warned = [d for d in decisions if d.has_warning]
        self.plain = [d for d in decisions if not d.has_warning]

    def _confirm(self, decision, choice=None):
        choice = choice or "%s%d" % (
            decision_compiler.CHOICE_CANDIDATE_PREFIX, decision.interpretations.first().id,
        )
        self.client.post(self.url, {"action": "save", "choice_%d" % decision.id: choice})

    def _confirm_all(self):
        for decision in self.warned:
            self._confirm(decision)

    # ---- 고르지 않은 항목 ----

    def test_publish_button_works_before_every_warned_item_is_confirmed(self):
        r = self.client.get(self.url)
        self.assertTrue(r.context["can_publish"])
        self.assertEqual(r.context["blocker_count"], 2)
        self.assertContains(r, "공개하면 빠져요")
        self._confirm(self.warned[0])
        self.assertEqual(self.client.get(self.url).context["blocker_count"], 1)

    def test_unchosen_warned_items_are_dropped_when_publishing(self):
        self._confirm(self.warned[0])
        r = self.client.post(self.url, {"action": "publish"})
        self.assertRedirects(r, self.url)
        self.assertFalse(Decision.objects.filter(pk=self.warned[1].pk).exists())
        self.assertEqual(
            set(self.meeting.decisions.values_list("pk", flat=True)), {self.warned[0].pk, self.plain[0].pk})
        self.assertFalse(self.meeting.decisions.filter(status=Decision.STATUS_DRAFT).exists())

    def test_saving_does_not_drop_unchosen_items(self):
        self.client.post(self.url, {"action": "save"})
        self.assertEqual(self.meeting.decisions.count(), 3)

    def test_a_carried_item_is_not_dropped(self):
        self._confirm(self.warned[0], decision_compiler.CHOICE_UNDECIDED)
        self.client.post(self.url, {"action": "publish"})
        self.assertEqual(Decision.objects.get(pk=self.warned[0].pk).status, Decision.STATUS_UNDECIDED)
        self.assertFalse(Decision.objects.filter(pk=self.warned[1].pk).exists())

    def test_publishing_refuses_when_nothing_would_be_left(self):
        """고르지 않아 빠지는 것까지 합쳐 공개할 항목이 하나도 남지 않으면 빼지 않고 사유를 보여준다."""
        self.plain[0].delete()
        r = self.client.post(self.url, {"action": "publish"})
        self.assertContains(r, "고른 결정이 하나도 없어")
        self.assertEqual(self.meeting.decisions.count(), 2)
        self.assertFalse(self.meeting.decisions.exclude(status=Decision.STATUS_DRAFT).exists())

    def test_an_invalid_choice_keeps_unchosen_items(self):
        """다른 항목의 선택이 잘못돼 공개가 실패하면, 고르지 않은 항목도 빼지 않는다(삭제는 되돌릴 수 없다)."""
        r = self.client.post(self.url, {
            "action": "publish",
            "choice_%d" % self.warned[0].id: decision_compiler.CHOICE_CUSTOM,
            "custom_text_%d" % self.warned[0].id: "",
        })
        self.assertContains(r, "한 줄 적어 주세요")
        self.assertEqual(self.meeting.decisions.count(), 3)

    def test_publishing_with_nothing_in_draft_is_refused(self):
        self._confirm_all()
        self.client.post(self.url, {"action": "publish"})
        r = self.client.post(self.url, {"action": "publish"})
        self.assertContains(r, "공개할 항목이 없습니다")

    def test_an_item_without_a_sentence_is_never_published(self):
        """문장이 빈 항목은 어떤 경로로 들어왔든 공개되지 않는다 — 고르지 않았으면 공개 때 빠진다.

        팀원 화면은 final_text 하나만 보여주므로, 문장이 없으면 번호만 있는 빈 줄이 공개된다.
        """
        self._confirm_all()
        blank = Decision.objects.create(
            meeting=self.meeting, order=90, what="문장이 비어 있는 항목",
            warnings=[], divergence_reason="", final_text="",
        )
        self.assertIn(blank, decision_compiler.publish_blockers(
            list(self.meeting.decisions.filter(status=Decision.STATUS_DRAFT))))
        self.client.post(self.url, {"action": "publish"})
        self.assertFalse(Decision.objects.filter(pk=blank.pk).exists())
        self.assertFalse(self.meeting.decisions.filter(final_text="").exists())

    # ---- 공개 ----

    def test_publishing_covers_items_without_warnings_too(self):
        self._confirm_all()
        r = self.client.post(self.url, {"action": "publish"})
        self.assertRedirects(r, self.url)
        for decision in self.meeting.decisions.all():
            self.assertEqual(decision.status, Decision.STATUS_PUBLISHED, decision.what)
        # 확인이 필요 없던 항목은 AI가 쓴 확정 문장이 그대로 공개된다.
        plain = self.meeting.decisions.get(pk=self.plain[0].pk)
        self.assertTrue(plain.final_text)
        self.assertEqual(plain.published_text, plain.final_text)

    def test_items_marked_undecided_become_undecided_not_published(self):
        self._confirm(self.warned[0])
        self._confirm(self.warned[1], choice=decision_compiler.CHOICE_UNDECIDED)
        self.client.post(self.url, {"action": "publish"})
        self.assertEqual(
            Decision.objects.get(pk=self.warned[1].pk).status, Decision.STATUS_UNDECIDED,
        )
        self.assertEqual(
            Decision.objects.get(pk=self.warned[0].pk).status, Decision.STATUS_PUBLISHED,
        )

    def test_publish_counts_are_reported_back(self):
        self._confirm(self.warned[0])
        self._confirm(self.warned[1], choice=decision_compiler.CHOICE_UNDECIDED)
        self.client.post(self.url, {"action": "publish"})
        r = self.client.get(self.url)
        self.assertEqual(r.context["published_count"], 2)
        self.assertEqual(r.context["undecided_count"], 1)
        self.assertEqual(r.context["draft_count"], 0)

    def test_specs_are_locked_after_publishing(self):
        self._confirm_all()
        self.client.post(self.url, {"action": "publish"})
        decision = Decision.objects.get(pk=self.warned[0].pk)
        before = decision.who
        self.client.post(self.url, {
            "action": "save",
            "who_%d" % decision.id: "바뀐 담당자",
            "what_%d" % decision.id: decision.what,
        })
        decision.refresh_from_db()
        self.assertEqual(decision.who, before)
        self.assertFalse(self.client.get(self.url).context["editable"])

    # ---- 공개 전에는 팀원 쪽 조회에 담기지 않는다 ----

    def test_draft_items_never_appear_in_the_member_facing_query(self):
        # 팀원 화면(Phase C)은 반드시 published_decisions()를 거친다.
        self.assertEqual(decision_compiler.published_decisions(self.meeting), [])
        self._confirm_all()
        self.client.post(self.url, {"action": "publish"})
        self.assertEqual(len(decision_compiler.published_decisions(self.meeting)), 3)

    def test_the_member_facing_query_includes_undecided_items(self):
        self._confirm(self.warned[0])
        self._confirm(self.warned[1], choice=decision_compiler.CHOICE_UNDECIDED)
        self.client.post(self.url, {"action": "publish"})
        statuses = {d.status for d in decision_compiler.published_decisions(self.meeting)}
        self.assertEqual(statuses, {Decision.STATUS_PUBLISHED, Decision.STATUS_UNDECIDED})

    # ---- 권한 ----

    def test_non_host_cannot_publish(self):
        self._confirm_all()
        client = Client()
        client.force_login(self.member)
        self.assertEqual(client.post(self.url, {"action": "publish"}).status_code, 403)
        self.assertFalse(self.meeting.decisions.exclude(status=Decision.STATUS_DRAFT).exists())

    def test_outsider_cannot_publish(self):
        client = Client()
        client.force_login(self.outsider)
        self.assertEqual(client.post(self.url, {"action": "publish"}).status_code, 404)


class DecisionReviseTests(TestCase):
    """공개 뒤 확정 문장을 고치면 '수정됨'으로 표시하고 팀원에게 수정 알림을 보낸다."""

    def setUp(self):
        self.host = User.objects.create_user("hostuser", password="pw12345!")
        self.alice = User.objects.create_user("aliceuser", password="pw12345!")
        self.bob = User.objects.create_user("bobuser", password="pw12345!")
        self.team = Team.objects.create(name="t")
        self.team.members.add(self.host, self.alice, self.bob)
        self.meeting = Meeting.objects.create(
            team=self.team, title="m", host=self.host,
            processing_status=Meeting.STATUS_DONE, record="결정", transcript="전사",
        )
        self.decision = Decision.objects.create(
            meeting=self.meeting, order=0, who="민수", what="배포", due_text="금요일",
            warnings=["완료 기준 빈칸"], status=Decision.STATUS_PUBLISHED,
            final_text="금요일 18시까지 스테이징 배포",
        )
        self.other = Decision.objects.create(
            meeting=self.meeting, order=1, what="문서 정리",
            warnings=["담당자 빈칸"], status=Decision.STATUS_PUBLISHED, final_text="다음 주 수요일까지",
        )
        self.url = reverse("decision_review", args=[self.meeting.id])
        self.client.force_login(self.host)

    def _revise(self, decision, text):
        return self.client.post(self.url, {"action": "revise", "decision_id": decision.id, "final_text": text})

    def test_revising_changes_the_sentence_and_sets_the_revised_flag(self):
        self._revise(self.decision, "월요일 10시까지")
        self.decision.refresh_from_db()
        self.assertEqual(self.decision.final_text, "월요일 10시까지")
        self.assertTrue(self.decision.revised)

    def test_revising_notifies_every_member_except_the_host(self):
        self._revise(self.decision, "월요일 10시까지")
        notes = Notification.objects.filter(meeting=self.meeting, kind=Notification.KIND_REVISED)
        self.assertEqual(sorted(n.user.username for n in notes), ["aliceuser", "bobuser"])
        self.assertTrue(all(n.is_unread for n in notes))

    def test_revising_clears_leftover_agreements(self):
        # 예전 '확인했어요' 기록은 바뀐 문장과 맞지 않으므로 함께 지운다.
        DecisionAgreement.objects.create(decision=self.decision, user=self.alice)
        self._revise(self.decision, "월요일 10시까지")
        self.assertEqual(self.decision.agreements.count(), 0)

    def test_revising_one_item_leaves_other_items_alone(self):
        self._revise(self.decision, "월요일 10시까지")
        self.other.refresh_from_db()
        self.assertFalse(self.other.revised)
        self.assertEqual(self.other.final_text, "다음 주 수요일까지")

    def test_revising_with_the_same_sentence_changes_nothing(self):
        self._revise(self.decision, self.decision.final_text)
        self.decision.refresh_from_db()
        self.assertFalse(self.decision.revised)
        self.assertFalse(Notification.objects.exists())

    def test_revising_requires_a_sentence(self):
        r = self._revise(self.decision, "   ")
        self.assertContains(r, "한 줄 적어 주세요")
        self.decision.refresh_from_db()
        self.assertEqual(self.decision.final_text, "금요일 18시까지 스테이징 배포")
        self.assertFalse(Notification.objects.exists())

    def test_revising_a_draft_item_is_refused(self):
        draft = Decision.objects.create(
            meeting=self.meeting, order=2, what="검토 중", warnings=["담당자 빈칸"],
            status=Decision.STATUS_DRAFT, final_text="초안 문장",
        )
        self.assertEqual(self._revise(draft, "바꾼 문장").status_code, 404)
        draft.refresh_from_db()
        self.assertEqual(draft.final_text, "초안 문장")

    def test_revising_an_undecided_item_is_refused(self):
        Decision.objects.filter(pk=self.decision.pk).update(status=Decision.STATUS_UNDECIDED)
        self.assertEqual(self._revise(self.decision, "바꾼 문장").status_code, 404)

    def test_revising_an_item_from_another_meeting_is_refused(self):
        other_meeting = Meeting.objects.create(
            team=self.team, title="m2", host=self.host, processing_status=Meeting.STATUS_DONE,
        )
        stranger = Decision.objects.create(
            meeting=other_meeting, order=0, what="남의 회의",
            status=Decision.STATUS_PUBLISHED, final_text="남의 문장",
        )
        self.assertEqual(self._revise(stranger, "바꾼 문장").status_code, 404)

    def test_non_host_cannot_revise(self):
        client = Client()
        client.force_login(self.alice)
        r = client.post(self.url, {
            "action": "revise", "decision_id": self.decision.id, "final_text": "바꾼 문장",
        })
        self.assertEqual(r.status_code, 403)
        self.decision.refresh_from_db()
        self.assertEqual(self.decision.final_text, "금요일 18시까지 스테이징 배포")

    def test_the_review_screen_offers_the_revise_form_only_after_publishing(self):
        self.assertContains(self.client.get(self.url), "확정 문장 수정")
        Decision.objects.filter(meeting=self.meeting).update(status=Decision.STATUS_DRAFT)
        self.assertNotContains(self.client.get(self.url), "확정 문장 수정")

    def test_the_review_screen_marks_a_revised_item(self):
        self._revise(self.decision, "월요일 10시까지")
        self.assertContains(self.client.get(self.url), "팀원에게 알림을 보냈어요")

    def test_the_review_screen_shows_no_member_tally(self):
        # 팀원 확인을 받지 않으므로 미확인 명단·응답 집계가 화면에 없다.
        r = self.client.get(self.url)
        self.assertNotContains(r, "아직 확인하지 않은 팀원")
        self.assertNotContains(r, "명 응답")
        for name in (self.alice.username, self.bob.username):
            self.assertNotContains(r, name)


class DecisionUnpublishTests(TestCase):
    """공개 취소 — 잘못 누른 공개를 수습하는 통로. 팀원 확인을 받지 않으므로 언제든 된다."""

    def setUp(self):
        self.host = User.objects.create_user("hostuser", password="pw12345!")
        self.member = User.objects.create_user("memberuser", password="pw12345!")
        self.team = Team.objects.create(name="t")
        self.team.members.add(self.host, self.member)
        self.meeting = Meeting.objects.create(
            team=self.team, title="m", host=self.host,
            processing_status=Meeting.STATUS_DONE, record="결정", transcript="전사",
        )
        self.decision = Decision.objects.create(
            meeting=self.meeting, order=0, who="민수", what="배포", due_text="금요일",
            warnings=["완료 기준 빈칸"], status=Decision.STATUS_PUBLISHED, final_text="금요일 18시까지",
        )
        self.url = reverse("decision_review", args=[self.meeting.id])
        self.client.force_login(self.host)

    def test_unpublishing_puts_items_back_into_draft(self):
        r = self.client.post(self.url, {"action": "unpublish"})
        self.assertRedirects(r, self.url)
        self.decision.refresh_from_db()
        self.assertEqual(self.decision.status, Decision.STATUS_DRAFT)
        # 확정 내용은 남겨 둔다 — 공개만 되돌리는 것이고 다시 고를 필요는 없다.
        self.assertEqual(self.decision.final_text, "금요일 18시까지")
        self.assertTrue(self.client.get(self.url).context["editable"])

    def test_the_choice_section_comes_back_after_unpublishing(self):
        # 이게 공개 취소를 넣은 이유다 — 공개 뒤에는 확정 선택 UI가 사라진다.
        radio = 'name="choice_%d"' % self.decision.id
        self.assertNotIn(radio, self.client.get(self.url).content.decode())
        self.client.post(self.url, {"action": "unpublish"})
        self.assertIn(radio, self.client.get(self.url).content.decode())

    def test_undecided_items_are_restored_too(self):
        Decision.objects.filter(pk=self.decision.pk).update(
            status=Decision.STATUS_UNDECIDED, marked_undecided=True,
        )
        self.client.post(self.url, {"action": "unpublish"})
        self.decision.refresh_from_db()
        self.assertEqual(self.decision.status, Decision.STATUS_DRAFT)
        self.assertTrue(self.decision.marked_undecided)

    def test_the_revised_flag_is_cleared(self):
        Decision.objects.filter(pk=self.decision.pk).update(revised=True)
        self.client.post(self.url, {"action": "unpublish"})
        self.decision.refresh_from_db()
        self.assertFalse(self.decision.revised)

    def test_unpublishing_works_even_after_members_have_looked(self):
        Notification.objects.create(
            user=self.member, meeting=self.meeting, kind=Notification.KIND_PUBLISHED,
            message="공개됨", read_at=timezone.now(),
        )
        self.client.post(self.url, {"action": "unpublish"})
        self.decision.refresh_from_db()
        self.assertEqual(self.decision.status, Decision.STATUS_DRAFT)

    def test_unpublishing_withdraws_unread_notifications(self):
        Notification.objects.create(user=self.member, meeting=self.meeting, kind=Notification.KIND_PUBLISHED, message="공개됨")
        read = Notification.objects.create(
            user=self.member, meeting=self.meeting, kind=Notification.KIND_PUBLISHED,
            message="예전 공개", read_at=timezone.now(),
        )
        self.client.post(self.url, {"action": "unpublish"})
        self.assertEqual(list(Notification.objects.all()), [read])

    def test_the_button_is_hidden_while_still_in_draft(self):
        Decision.objects.filter(pk=self.decision.pk).update(status=Decision.STATUS_DRAFT)
        r = self.client.get(self.url)
        self.assertFalse(r.context["can_unpublish"])
        self.assertNotContains(r, 'value="unpublish"')

    def test_unpublishing_with_nothing_published_is_refused(self):
        Decision.objects.filter(pk=self.decision.pk).update(status=Decision.STATUS_DRAFT)
        r = self.client.post(self.url, {"action": "unpublish"})
        self.assertContains(r, "공개된 항목이 없습니다")

    def test_reset_message_points_at_unpublishing(self):
        # 공개된 뒤 "결정 지우고 다시 추출"을 시도하면 다음에 뭘 눌러야 하는지 알려줘야 한다.
        r = self.client.post(self.url, {"action": "reset"})
        self.assertContains(r, "이미 공개된 항목이 있어 결정 사항을 지울 수 없습니다")
        self.assertContains(r, "공개 취소")
        self.assertEqual(self.meeting.decisions.count(), 1)

    def test_non_host_cannot_unpublish(self):
        client = Client()
        client.force_login(self.member)
        self.assertEqual(client.post(self.url, {"action": "unpublish"}).status_code, 403)
        self.decision.refresh_from_db()
        self.assertEqual(self.decision.status, Decision.STATUS_PUBLISHED)


class DecisionAckTests(TestCase):
    """팀원 화면(decision_ack) — 공개된 결정을 읽기만 하고, 근거가 된 전사 원문 위치를 받는다."""

    TRANSCRIPT = "회의 시작할게요. 민수가 금요일까지 배포를 맡기로 했습니다. 문서는 다음 주에 보기로 해요."

    def setUp(self):
        self.host = User.objects.create_user("hostuser", password="pw12345!")
        self.alice = User.objects.create_user("aliceuser", password="pw12345!")
        self.bob = User.objects.create_user("bobuser", password="pw12345!")
        self.team = Team.objects.create(name="t")
        self.team.members.add(self.host, self.alice, self.bob)
        self.meeting = Meeting.objects.create(
            team=self.team, title="m", host=self.host,
            processing_status=Meeting.STATUS_DONE, record="결정", transcript=self.TRANSCRIPT,
        )
        self.decision = Decision.objects.create(
            meeting=self.meeting, order=0, who="민수", what="배포",
            warnings=["완료 기준 빈칸"], status=Decision.STATUS_PUBLISHED, final_text="금요일 18시까지 스테이징 배포",
        )
        self.url = reverse("decision_ack", args=[self.meeting.id])
        self.review_url = reverse("decision_review", args=[self.meeting.id])

    def _as(self, user):
        client = Client()
        client.force_login(user)
        return client

    def test_member_sees_the_published_text(self):
        self.assertContains(self._as(self.alice).get(self.url), "금요일 18시까지 스테이징 배포")

    def test_the_screen_is_read_only(self):
        r = self._as(self.alice).get(self.url)
        self.assertNotContains(r, "확인했어요")
        self.assertNotContains(r, "제 이해와 달랐어요")
        self.assertNotContains(r, '<form method="post"')

    def test_posting_changes_nothing(self):
        r = self._as(self.alice).post(self.url, {"decision_id": self.decision.id, "kind": "ack"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(DecisionAgreement.objects.exists())

    def test_draft_items_are_not_in_the_member_response_before_publishing(self):
        Decision.objects.create(
            meeting=self.meeting, order=1, who="지은", what="비공개 초안 작업",
            warnings=["담당자 빈칸"], status=Decision.STATUS_DRAFT, final_text="아직 공개 안 된 문장",
        )
        r = self._as(self.alice).get(self.url)
        self.assertNotContains(r, "아직 공개 안 된 문장")
        self.assertNotContains(r, "비공개 초안 작업")
        self.assertEqual(len(r.context["rows"]), 1)

    def test_nothing_published_shows_the_empty_state(self):
        Decision.objects.filter(pk=self.decision.pk).update(status=Decision.STATUS_DRAFT)
        r = self._as(self.alice).get(self.url)
        self.assertNotContains(r, "금요일 18시까지 스테이징 배포")
        self.assertContains(r, "아직 공개된 결정이 없습니다")

    def test_undecided_items_are_shown_as_carried_over(self):
        Decision.objects.create(
            meeting=self.meeting, order=1, what="미정 안건", status=Decision.STATUS_UNDECIDED,
            marked_undecided=True, final_text="미정 안건은 다음 회의에서 다시 정한다",
        )
        self.assertContains(self._as(self.alice).get(self.url), "미결정")

    def test_revised_items_are_marked(self):
        self.assertNotContains(self._as(self.alice).get(self.url), "수정됨")
        Decision.objects.filter(pk=self.decision.pk).update(revised=True)
        self.assertContains(self._as(self.alice).get(self.url), "수정됨")

    def test_items_are_shown_one_at_a_time_with_prev_next(self):
        r = self._as(self.alice).get(self.url)
        self.assertContains(r, 'class="decision-step item" data-decision-id="%d"' % self.decision.id)
        self.assertContains(r, 'id="step-prev"')
        self.assertContains(r, 'id="step-next"')
        self.assertContains(r, 'id="transcript-panel"')

    def test_the_chosen_candidates_evidence_is_highlighted(self):
        evidence = "민수가 금요일까지 배포를 맡기로 했습니다."
        Interpretation.objects.create(decision=self.decision, order=0, text="다른 후보", evidence="문서는 다음 주에 보기로 해요.")
        Interpretation.objects.create(decision=self.decision, order=1, text=self.decision.final_text, evidence=evidence)
        spans = self._as(self.alice).get(self.url).context["evidence_data"][str(self.decision.id)]
        self.assertEqual(len(spans), 1)
        # 하이라이트는 근거의 마지막 글자에서 끝난다(문장부호는 대조에서 빠진다).
        self.assertEqual(self.TRANSCRIPT[spans[0]["start"]:spans[0]["end"]], evidence.rstrip("."))

    def test_a_clear_decisions_stored_evidence_is_highlighted(self):
        Decision.objects.filter(pk=self.decision.pk).update(evidence="문서는 다음 주에 보기로 해요")
        spans = self._as(self.alice).get(self.url).context["evidence_data"][str(self.decision.id)]
        self.assertEqual(self.TRANSCRIPT[spans[0]["start"]:spans[0]["end"]], "문서는 다음 주에 보기로 해요")

    def test_opening_the_screen_deletes_this_meetings_notifications(self):
        other_meeting = Meeting.objects.create(team=self.team, title="m2", host=self.host, processing_status=Meeting.STATUS_DONE)
        mine = Notification.objects.create(user=self.alice, meeting=self.meeting, kind=Notification.KIND_PUBLISHED, message="공개")
        elsewhere = Notification.objects.create(user=self.alice, meeting=other_meeting, kind=Notification.KIND_PUBLISHED, message="공개")
        bobs = Notification.objects.create(user=self.bob, meeting=self.meeting, kind=Notification.KIND_PUBLISHED, message="공개")
        self._as(self.alice).get(self.url)
        self.assertFalse(Notification.objects.filter(pk=mine.pk).exists())
        self.assertTrue(Notification.objects.filter(pk=elsewhere.pk).exists())
        self.assertTrue(Notification.objects.filter(pk=bobs.pk).exists())

    def test_host_is_sent_to_the_review_screen(self):
        self.assertRedirects(self._as(self.host).get(self.url), self.review_url)

    def test_outsider_gets_404(self):
        outsider = User.objects.create_user("outsider", password="pw12345!")
        self.assertEqual(self._as(outsider).get(self.url).status_code, 404)

    def test_unfinished_record_gets_404(self):
        Meeting.objects.filter(pk=self.meeting.pk).update(processing_status=Meeting.STATUS_PROCESSING)
        self.assertEqual(self._as(self.alice).get(self.url).status_code, 404)

    def test_member_cannot_open_the_review_screen(self):
        self.assertEqual(self._as(self.alice).get(self.review_url).status_code, 403)


class DecisionNotificationTests(TestCase):
    """공개·수정 알림과 그 알림을 보여주는 세 곳 — 상단 메뉴 아이콘·알림 목록, 회의 목록 배지, 회의 화면 안내."""

    def setUp(self):
        self.host = User.objects.create_user("dechost", password="pw12345!")
        self.alpha = User.objects.create_user("dec-alpha", password="pw12345!")
        self.beta = User.objects.create_user("dec-beta", password="pw12345!")
        self.outsider = User.objects.create_user("outsider", password="pw12345!")
        self.team = Team.objects.create(name="t", created_by=self.host)
        self.team.members.add(self.host, self.alpha, self.beta)
        self.meeting = Meeting.objects.create(
            team=self.team, title="첫 회의", host=self.host,
            processing_status=Meeting.STATUS_DONE, record="회의록", transcript="전사본",
        )
        self.decision = Decision.objects.create(
            meeting=self.meeting, order=0, what="배포", final_text="금요일까지 배포한다",
        )
        self.review_url = reverse("decision_review", args=[self.meeting.id])

    def _as(self, user):
        client = Client()
        client.force_login(user)
        return client

    def _publish(self):
        return self._as(self.host).post(self.review_url, {"action": "publish"})

    def test_publishing_notifies_members_but_not_the_host(self):
        self._publish()
        notes = Notification.objects.filter(meeting=self.meeting)
        self.assertEqual(sorted(n.user.username for n in notes), ["dec-alpha", "dec-beta"])
        self.assertTrue(all(n.kind == Notification.KIND_PUBLISHED for n in notes))
        self.assertIn("첫 회의", notes[0].message)

    def test_a_refused_publish_sends_nothing(self):
        Decision.objects.filter(pk=self.decision.pk).update(final_text="", warnings=["모호"])
        self._publish()
        self.assertFalse(Notification.objects.exists())

    def test_the_bell_counts_unread_notifications(self):
        self._publish()
        r = self._as(self.alpha).get(reverse("index"))
        self.assertEqual(r.context["unread_notifications"], 1)
        self.assertContains(r, 'class="notify-count">1<')
        self.assertNotContains(self._as(self.host).get(reverse("index")), 'class="notify-count"')

    def test_the_notification_list_links_to_the_decision_screen(self):
        self._publish()
        r = self._as(self.alpha).get(reverse("notification_list"))
        self.assertContains(r, "첫 회의")
        self.assertContains(r, reverse("decision_ack", args=[self.meeting.id]))
        self.assertContains(r, "새 알림")

    def test_read_all_clears_the_bell(self):
        self._publish()
        client = self._as(self.alpha)
        client.post(reverse("notification_list"), {"action": "read_all"})
        self.assertEqual(client.get(reverse("index")).context["unread_notifications"], 0)

    def test_the_list_only_shows_my_own_notifications(self):
        self._publish()
        self.assertNotContains(self._as(self.outsider).get(reverse("notification_list")), "첫 회의")

    def test_the_meeting_list_badges_meetings_with_unread_notifications(self):
        self._publish()
        r = self._as(self.alpha).get(reverse("meeting_list", args=[self.team.id]))
        self.assertEqual([row["notice_label"] for row in r.context["rows"]], ["새 결정"])
        self.assertContains(r, "새 결정")

    def _revise(self, text):
        return self._as(self.host).post(self.review_url, {
            "action": "revise", "decision_id": self.decision.id, "final_text": text,
        })

    def test_a_revision_after_reading_shows_as_revised_in_the_meeting_list(self):
        self._publish()
        self._as(self.alpha).get(reverse("decision_ack", args=[self.meeting.id]))
        self._revise("월요일까지 배포한다")
        r = self._as(self.alpha).get(reverse("meeting_list", args=[self.team.id]))
        self.assertEqual(r.context["rows"][0]["notice_label"], "수정됨")

    def test_a_revision_before_reading_stays_a_new_decision(self):
        # 아직 결정을 본 적이 없으면 바뀐 결정이 아니라 처음 보는 결정이다.
        self._publish()
        self._revise("월요일까지 배포한다")
        r = self._as(self.alpha).get(reverse("meeting_list", args=[self.team.id]))
        self.assertEqual(r.context["rows"][0]["notice_label"], "새 결정")

    def test_repeated_revisions_keep_one_unread_notification_per_member(self):
        self._publish()
        first = Notification.objects.get(user=self.alpha)
        Notification.objects.filter(pk=first.pk).update(created_at=timezone.now() - timedelta(hours=1))
        self._revise("월요일까지 배포한다")
        self._revise("화요일까지 배포한다")
        notes = Notification.objects.filter(user=self.alpha, meeting=self.meeting)
        self.assertEqual(notes.count(), 1)
        self.assertGreater(notes.get().created_at, timezone.now() - timedelta(minutes=1))
        self.assertEqual(self._as(self.alpha).get(reverse("index")).context["unread_notifications"], 1)

    def test_revisions_after_reading_leave_one_new_notification(self):
        self._publish()
        self._as(self.alpha).get(reverse("decision_ack", args=[self.meeting.id]))
        # 읽은 알림은 지워진다.
        self.assertFalse(Notification.objects.filter(user=self.alpha).exists())
        self._revise("월요일까지 배포한다")
        self._revise("화요일까지 배포한다")
        note = Notification.objects.get(user=self.alpha, meeting=self.meeting)
        self.assertEqual(note.kind, Notification.KIND_REVISED)
        self.assertTrue(note.is_unread)

    def test_read_all_deletes_my_notifications_only(self):
        self._publish()
        self._as(self.alpha).post(reverse("notification_list"), {"action": "read_all"})
        self.assertFalse(Notification.objects.filter(user=self.alpha).exists())
        self.assertTrue(Notification.objects.filter(user=self.beta).exists())

    def test_old_read_rows_are_not_listed(self):
        Notification.objects.create(user=self.alpha, meeting=self.meeting, kind=Notification.KIND_PUBLISHED,
                                    message="예전에 읽은 알림", read_at=timezone.now())
        self.assertNotContains(self._as(self.alpha).get(reverse("notification_list")), "예전에 읽은 알림")

    def test_republishing_after_reset_notifies_again(self):
        """공개 → 공개 취소 → 결정 지우기 → 다시 컴파일 → 공개해도 참석자에게 알림이 간다."""
        self._publish()
        host = self._as(self.host)
        host.post(self.review_url, {"action": "unpublish"})
        self.assertFalse(Notification.objects.exists())
        host.post(self.review_url, {"action": "reset"})
        with patch.dict(os.environ, {}, clear=True):
            host.post(self.review_url, {"action": "compile"})
        host.post(self.review_url, {"action": "publish"})
        self.assertEqual(sorted(n.user.username for n in Notification.objects.filter(kind=Notification.KIND_PUBLISHED)),
                         ["dec-alpha", "dec-beta"])

    def test_only_participants_are_notified(self):
        self.meeting.participants.set([self.host, self.alpha])
        self._publish()
        self.assertEqual([n.user for n in Notification.objects.all()], [self.alpha])

    def test_a_meeting_without_participants_notifies_the_whole_team(self):
        self._publish()
        self.assertEqual(Notification.objects.count(), 2)

    def test_a_participant_who_left_the_team_is_not_notified(self):
        self.meeting.participants.set([self.host, self.alpha, self.beta])
        self.team.members.remove(self.beta)
        self._publish()
        self.assertEqual([n.user for n in Notification.objects.all()], [self.alpha])

    def test_the_meeting_screen_shows_a_banner_until_the_decisions_are_opened(self):
        self._publish()
        client = self._as(self.alpha)
        detail = reverse("meeting_detail", args=[self.meeting.id])
        self.assertContains(client.get(detail), "이 회의의 결정이 공개됐어요")
        client.get(reverse("decision_ack", args=[self.meeting.id]))
        self.assertNotContains(client.get(detail), "이 회의의 결정이 공개됐어요")
        self.assertEqual(client.get(reverse("meeting_list", args=[self.team.id])).context["rows"][0]["notice"], None)

    def test_the_meeting_list_badges_take_a_fixed_number_of_queries(self):
        for i in range(3):
            Meeting.objects.create(team=self.team, title="m%d" % i, host=self.host)
        self._publish()
        client = self._as(self.alpha)
        url = reverse("meeting_list", args=[self.team.id])
        client.get(url)
        with CaptureQueriesContext(connection) as few:
            client.get(url)
        for i in range(5):
            Meeting.objects.create(team=self.team, title="n%d" % i, host=self.host)
        with CaptureQueriesContext(connection) as many:
            client.get(url)
        self.assertEqual(len(few), len(many))


class DecisionEvidenceTests(TestCase):
    """전사 원본 창의 하이라이트 위치 — 근거 문장이 원문 어디에 있는지."""

    SOURCE = "화자 A: 회의 시작할게요.\n화자 B: 민수가, 금요일까지 배포를 맡기로 했습니다!\n화자 A: 좋아요."

    def test_the_span_covers_the_evidence_even_with_different_spacing_and_punctuation(self):
        start, end = decision_compiler.evidence_span(self.SOURCE, "민수가 금요일까지 배포를 맡기로 했습니다")
        self.assertEqual(self.SOURCE[start:end], "민수가, 금요일까지 배포를 맡기로 했습니다")

    def test_missing_evidence_has_no_span(self):
        self.assertIsNone(decision_compiler.evidence_span(self.SOURCE, "회의에 없던 문장"))
        self.assertIsNone(decision_compiler.evidence_span(self.SOURCE, ""))
        self.assertIsNone(decision_compiler.evidence_span("", "회의 시작"))

    def test_the_same_sentence_for_two_candidates_is_highlighted_once_with_both_labels(self):
        spans = decision_compiler.evidence_spans(self.SOURCE, [("후보 A", "좋아요"), ("후보 B", "좋아요"), ("후보 C", "없는 문장")])
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0]["label"], "후보 A · 후보 B")

    def test_spans_are_sorted_and_never_overlap(self):
        spans = decision_compiler.evidence_spans(self.SOURCE, [
            ("뒤", "좋아요"), ("앞", "회의 시작할게요"), ("겹침", "회의 시작"),
        ])
        self.assertEqual([span["label"] for span in spans], ["앞", "뒤"])

    def test_compiling_stores_the_evidence_of_clear_decisions(self):
        host = User.objects.create_user("host", password="pw12345!")
        team = Team.objects.create(name="t")
        team.members.add(host)
        meeting = Meeting.objects.create(
            team=team, title="m", host=host, processing_status=Meeting.STATUS_DONE,
            transcript="민수가 금요일까지 배포를 맡기로 했습니다.",
        )
        raw = json.dumps([{
            "topic": "배포", "ambiguous": False, "final": "민수가 금요일까지 배포한다",
            "evidence": "민수가 금요일까지 배포를 맡기로 했습니다.",
        }], ensure_ascii=False)
        with patch.dict(os.environ, {"OLLAMA_MODEL": "m1"}), patch.object(services, "_generate", return_value=raw):
            decision_compiler.compile_meeting(meeting)
        self.assertEqual(meeting.decisions.get().evidence, "민수가 금요일까지 배포를 맡기로 했습니다.")

    def test_the_review_screen_sends_the_decisions_evidence_span(self):
        host = User.objects.create_user("host2", password="pw12345!")
        team = Team.objects.create(name="t2")
        team.members.add(host)
        meeting = Meeting.objects.create(
            team=team, title="m", host=host, processing_status=Meeting.STATUS_DONE, transcript=self.SOURCE,
        )
        decision = Decision.objects.create(meeting=meeting, order=0, what="배포", divergence_reason="기한이 갈려요",
                                           evidence="민수가 금요일까지 배포를 맡기로 했습니다")
        Interpretation.objects.create(decision=decision, order=0, text="금요일까지 배포한다", recommended=True)
        client = Client()
        client.force_login(host)
        r = client.get(reverse("decision_review", args=[meeting.id]))
        spans = r.context["evidence_data"][str(decision.id)]
        self.assertEqual([span["label"] for span in spans], ["근거"])
        self.assertContains(r, 'id="transcript-panel"')
        self.assertContains(r, 'data-evidence-label="근거"')
        self.assertContains(r, 'id="step-next"')

    def test_a_repeated_sentence_points_to_the_occurrence_near_the_decision(self):
        filler = "화자 C: 다른 이야기를 이어서 합니다.\n" * 8  # 맥락 창(80글자)보다 멀리 떨어뜨린다.
        source = (
            "화자 A: 점심 메뉴는 김밥으로 하죠.\n화자 B: 좋아요.\n" + filler +
            "화자 A: 배포는 민수가 금요일까지 맡아 주세요.\n화자 B: 좋아요.\n"
        )
        first, second = [m.start() for m in re.finditer("좋아요", source)]
        start, _ = decision_compiler.evidence_span(source, "좋아요", context="민수 금요일까지 배포")
        self.assertEqual(start, second)
        # 맥락이 없으면 예전처럼 처음 나온 자리.
        self.assertEqual(decision_compiler.evidence_span(source, "좋아요")[0], first)

    def test_every_occurrence_is_highlighted_but_only_one_is_primary(self):
        source = "가: 좋아요. 나: 배포는 금요일. 다: 좋아요."
        spans = decision_compiler.evidence_spans(source, [("", "좋아요")], context="금요일 배포")
        self.assertEqual(len(spans), 2)
        self.assertEqual([span["primary"] for span in spans].count(True), 1)

    def test_a_primary_span_wins_over_an_overlapping_repeat(self):
        # "좋아요"의 첫 자리는 대표가 아니고(맥락과 먼 곳), 그 자리에 걸친 "아요 시작"은 대표다.
        filler = " 음" * 120  # 맥락 창(80글자)보다 멀리 떨어뜨린다.
        source = "좋아요 시작" + filler + " 금요일 배포 좋아요"
        spans = decision_compiler.evidence_spans(
            source, [("가", "좋아요"), ("나", "아요 시작")], context="금요일 배포",
        )
        self.assertEqual([(span["label"], span["primary"]) for span in spans], [("나", True), ("가", True)])


class MeetingParticipantTests(TestCase):
    """회의 참석자 고르기 — 결정 알림을 받는 범위."""

    def setUp(self):
        self.host = User.objects.create_user("phost", password="pw12345!")
        self.alpha = User.objects.create_user("palpha", password="pw12345!")
        self.beta = User.objects.create_user("pbeta", password="pw12345!")
        self.outsider = User.objects.create_user("poutsider", password="pw12345!")
        self.team = Team.objects.create(name="pt")
        self.team.members.add(self.host, self.alpha, self.beta)
        self.client.force_login(self.host)

    def test_the_create_form_checks_every_member_by_default(self):
        r = self.client.get(reverse("meeting_create", args=[self.team.id]))
        self.assertEqual(r.context["participant_ids"], {self.host.id, self.alpha.id, self.beta.id})
        self.assertContains(r, 'name="participants"')

    def test_creating_saves_the_chosen_participants_and_always_the_host(self):
        self.client.post(reverse("meeting_create", args=[self.team.id]), {
            "title": "회의", "participants": [str(self.alpha.id), str(self.outsider.id), "x"],
        })
        meeting = Meeting.objects.get(title="회의")
        self.assertEqual(set(meeting.participants.all()), {self.host, self.alpha})

    def test_the_host_can_change_participants(self):
        meeting = Meeting.objects.create(team=self.team, title="m", host=self.host)
        r = self.client.post(reverse("meeting_detail", args=[meeting.id]), {
            "action": "set_participants", "participants": [str(self.beta.id)],
        })
        self.assertRedirects(r, reverse("meeting_detail", args=[meeting.id]))
        self.assertEqual(set(meeting.participants.all()), {self.host, self.beta})

    def test_a_member_cannot_change_participants(self):
        meeting = Meeting.objects.create(team=self.team, title="m", host=self.host)
        self.client.force_login(self.alpha)
        r = self.client.post(reverse("meeting_detail", args=[meeting.id]), {
            "action": "set_participants", "participants": [str(self.alpha.id)],
        })
        self.assertEqual(r.status_code, 403)
        self.assertFalse(meeting.participants.exists())

    def test_only_the_host_sees_the_participant_picker(self):
        meeting = Meeting.objects.create(team=self.team, title="m", host=self.host)
        url = reverse("meeting_detail", args=[meeting.id])
        self.assertContains(self.client.get(url), 'value="set_participants"')
        self.client.force_login(self.alpha)
        self.assertNotContains(self.client.get(url), 'value="set_participants"')
