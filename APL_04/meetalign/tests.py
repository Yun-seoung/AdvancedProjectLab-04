import io
import json
import os
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
from django.urls import reverse
from django.utils import timezone

from . import decision_compiler, quiz_status, services, tasks, views, whisper_worker
from .models import (
    Answer, ChatMessage, CheckQuestion, Checkpoint, Decision, DecisionAgreement, DecisionVote,
    Interpretation, Meeting, Question, Recording, SttUsage, Team,
)

# 셸에 실행용 LLM/STT 환경변수가 설정돼 있어도 테스트가 실제 모델/API를 호출하지 않고 항상 같은 결과가
# 나오도록, 모듈 시작 시 잠시 치웠다가 끝나면 되돌린다. 필요한 테스트는 각자 patch.dict로 값을 넣는다.
_MODEL_ENV_VARS = (
    "GEMINI_API_KEY", "GEMINI_MODEL", "GEMINI_TIMEOUT", "OLLAMA_MODEL", "MLX_WHISPER_MODEL",
    "FASTER_WHISPER_MODEL", "FASTER_WHISPER_DEVICE", "FASTER_WHISPER_COMPUTE_TYPE",
    "CLOVA_SPEECH_INVOKE_URL", "CLOVA_SPEECH_SECRET_KEY", "CLOVA_SPEECH_FREE_MINUTES", "CLOVA_SPEECH_DIARIZATION",
    "GROQ_API_KEY", "GROQ_MODEL",
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
                     "summary", "answers", "understanding"]:
            self.assertEqual(self.client.get(reverse(name, args=[self.meeting.id])).status_code, 200, name)

    def test_submit_and_end(self):
        url = reverse("meeting_detail", args=[self.meeting.id])
        self.assertRedirects(self.client.post(url, {"action": "submit"}),
                             reverse("meeting_result", args=[self.meeting.id]))
        self.assertRedirects(self.client.post(url, {"action": "end"}),
                             reverse("question_review", args=[self.meeting.id]))
        self.meeting.refresh_from_db()
        self.assertTrue(self.meeting.ended)

    def test_non_host_redirected_to_understanding_after_end(self):
        other = User.objects.create_user("b", password="pw12345!")
        self.team.members.add(other)
        member = Client()
        member.force_login(other)
        url = reverse("meeting_detail", args=[self.meeting.id])
        self.assertRedirects(member.post(url, {"action": "end"}),
                             reverse("understanding", args=[self.meeting.id]))

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
        self.assertContains(r, "가짜 LLM")
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
        for name in ["meeting_result", "chat", "inbox", "summary", "answers", "understanding"]:
            r = self.client.get(reverse(name, args=[self.meeting.id]))
            self.assertContains(r, back, msg_prefix=name)

    def test_question_review_creates_draft_questions_once(self):
        self.client.get(reverse("question_review", args=[self.meeting.id]))
        self.client.get(reverse("question_review", args=[self.meeting.id]))
        self.assertEqual(CheckQuestion.objects.filter(meeting=self.meeting).count(), 2)
        self.assertTrue(all(q.status == CheckQuestion.STATUS_DRAFT for q in CheckQuestion.objects.filter(meeting=self.meeting)))

    def test_questions_hidden_from_participants_until_published(self):
        self.client.get(reverse("question_review", args=[self.meeting.id]))
        r = self.client.get(reverse("understanding", args=[self.meeting.id]))
        self.assertContains(r, "아직 질문이 공개되지 않았습니다")

    def test_publish_makes_questions_visible_to_participants(self):
        self.client.get(reverse("question_review", args=[self.meeting.id]))
        self.client.post(reverse("question_review", args=[self.meeting.id]), {"action": "publish"})
        q = CheckQuestion.objects.filter(meeting=self.meeting).first()
        self.assertEqual(q.status, CheckQuestion.STATUS_PUBLISHED)
        r = self.client.get(reverse("understanding", args=[self.meeting.id]))
        self.assertContains(r, q.text)

    def test_host_can_edit_question_before_publishing(self):
        self.client.get(reverse("question_review", args=[self.meeting.id]))
        q = CheckQuestion.objects.filter(meeting=self.meeting).first()
        self.client.post(reverse("question_review", args=[self.meeting.id]), {
            "text_%d" % q.id: "수정된 문항", "evidence_%d" % q.id: "근거",
            "choice_%d_0" % q.id: "보기1", "choice_%d_1" % q.id: "보기2",
            "choice_%d_2" % q.id: "보기3", "choice_%d_3" % q.id: "보기4",
            "correct_choice_%d" % q.id: "2",
        })
        q.refresh_from_db()
        self.assertEqual(q.text, "수정된 문항")
        self.assertEqual(q.choices, ["보기1", "보기2", "보기3", "보기4"])
        self.assertEqual(q.correct_choice, 2)
        self.assertEqual(q.status, CheckQuestion.STATUS_DRAFT)

    def test_host_can_edit_all_questions_in_one_submit(self):
        self.client.get(reverse("question_review", args=[self.meeting.id]))
        questions = list(CheckQuestion.objects.filter(meeting=self.meeting))
        self.assertGreaterEqual(len(questions), 2)
        data = {}
        for i, q in enumerate(questions):
            data["text_%d" % q.id] = "수정된 문항%d" % i
            data["evidence_%d" % q.id] = ""
            for j in range(4):
                data["choice_%d_%d" % (q.id, j)] = "보기%d" % j
            data["correct_choice_%d" % q.id] = "1"
        self.client.post(reverse("question_review", args=[self.meeting.id]), data)
        for i, q in enumerate(questions):
            q.refresh_from_db()
            self.assertEqual(q.text, "수정된 문항%d" % i)
            self.assertEqual(q.correct_choice, 1)

    def _published_question(self, choices=None, correct_choice=0):
        return CheckQuestion.objects.create(
            meeting=self.meeting, text="Q", choices=choices or ["맞음", "틀림1", "틀림2", "틀림3"],
            correct_choice=correct_choice, status=CheckQuestion.STATUS_PUBLISHED,
        )

    def test_understanding_saves_and_prefills_answer(self):
        q = self._published_question()
        self.client.post(reverse("understanding", args=[self.meeting.id]), {"q%d" % q.id: "0"})
        self.assertEqual(Answer.objects.get(question=q, user=self.user).selected_choice, 0)
        r = self.client.get(reverse("understanding", args=[self.meeting.id]))
        self.assertContains(r, "checked")

    def test_understanding_resubmit_updates_not_duplicates(self):
        q = self._published_question()
        self.client.post(reverse("understanding", args=[self.meeting.id]), {"q%d" % q.id: "0"})
        self.client.post(reverse("understanding", args=[self.meeting.id]), {"q%d" % q.id: "1"})
        self.assertEqual(Answer.objects.filter(question=q, user=self.user).count(), 1)
        self.assertEqual(Answer.objects.get(question=q, user=self.user).selected_choice, 1)

    def test_same_question_can_hold_different_users_answers(self):
        # 핵심 검증: 같은 질문에 팀원마다 답이 따로 저장돼야 이해도 비교가 가능하다.
        other = User.objects.create_user("b")
        question = self._published_question()
        Answer.objects.create(question=question, user=self.user, selected_choice=0)
        Answer.objects.create(question=question, user=other, selected_choice=1)
        self.assertEqual(question.answers.count(), 2)

    def test_discrepancy_flagged_when_someone_picks_wrong_answer(self):
        other = User.objects.create_user("b")
        question = self._published_question()
        Answer.objects.create(question=question, user=self.user, selected_choice=0)
        Answer.objects.create(question=question, user=other, selected_choice=1)
        r = self.client.get(reverse("verification", args=[self.meeting.id]))
        self.assertContains(r, "a: 맞음")
        self.assertContains(r, "b: 틀림1")
        self.assertNotContains(r, "엇갈린 답변이 없습니다.")

    def test_no_discrepancy_when_everyone_picks_correct_answer(self):
        other = User.objects.create_user("b")
        question = self._published_question()
        Answer.objects.create(question=question, user=self.user, selected_choice=0)
        Answer.objects.create(question=question, user=other, selected_choice=0)
        r = self.client.get(reverse("verification", args=[self.meeting.id]))
        self.assertContains(r, "엇갈린 답변이 없습니다.")

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
            self.assertIn("(가짜 LLM 응답)", services.chat_reply(self.meeting, "q"))
            self.assertEqual(services.generate_check_questions(self.meeting), services.FAKE_CHECK_QUESTIONS)

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

    def _mcq_json(self, n=2):
        items = [
            {"question": "질문%d" % i, "choices": ["a", "b", "c", "d"], "answer_index": i % 4, "evidence": ""}
            for i in range(n)
        ]
        return json.dumps(items, ensure_ascii=False)

    def test_generate_check_questions_parses_llm_json(self):
        with patch.dict(os.environ, {"OLLAMA_MODEL": "m1"}), patch.object(services, "_generate", return_value=self._mcq_json(2)):
            result = services.generate_check_questions(self.meeting)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["text"], "질문0")
        self.assertEqual(result[0]["choices"], ["a", "b", "c", "d"])
        self.assertEqual(result[0]["correct_choice"], 0)
        self.assertEqual(result[0]["evidence"], "")

    def test_generate_check_questions_caps_at_five(self):
        with patch.dict(os.environ, {"OLLAMA_MODEL": "m1"}), patch.object(services, "_generate", return_value=self._mcq_json(8)):
            result = services.generate_check_questions(self.meeting)
        self.assertEqual(len(result), 5)

    def test_llm_failure_falls_back(self):
        with patch.dict(os.environ, {"OLLAMA_MODEL": "m1"}), patch.object(services, "_generate", side_effect=OSError("down")):
            self.assertIn("LLM 연결 실패", services.chat_reply(self.meeting, "q"))
            self.assertEqual(services.generate_check_questions(self.meeting), services.FAKE_CHECK_QUESTIONS)

    def test_no_transcript_uses_fake_even_when_enabled(self):
        self.meeting.transcript = ""
        self.meeting.save()
        with patch.dict(os.environ, {"OLLAMA_MODEL": "m1"}), patch.object(services, "_generate") as gen:
            self.assertEqual(services.generate_check_questions(self.meeting), services.FAKE_CHECK_QUESTIONS)
        gen.assert_not_called()

    def test_malformed_json_falls_back_to_fake(self):
        with patch.dict(os.environ, {"OLLAMA_MODEL": "m1"}), patch.object(services, "_generate", return_value="이건 JSON이 아닙니다"):
            self.assertEqual(services.generate_check_questions(self.meeting), services.FAKE_CHECK_QUESTIONS)

    def test_parse_mcq_json_drops_malformed_items_but_keeps_valid_ones(self):
        raw = json.dumps([
            {"question": "좋은 질문", "choices": ["a", "b", "c", "d"], "answer_index": 1, "evidence": "e"},
            {"question": "보기 3개뿐", "choices": ["a", "b", "c"], "answer_index": 0},
            {"question": "범위 밖 정답", "choices": ["a", "b", "c", "d"], "answer_index": 9},
            {"question": "", "choices": ["a", "b", "c", "d"], "answer_index": 0},
        ], ensure_ascii=False)
        result = services._parse_mcq_json(raw)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["text"], "좋은 질문")

    def test_parse_mcq_json_drops_items_with_duplicate_choices(self):
        raw = json.dumps([
            {"question": "보기 중복", "choices": ["같은 문장입니다.", "같은 문장입니다.", "다른 보기", "같은 문장입니다."], "answer_index": 0, "evidence": ""},
            {"question": "보기 전부 다름", "choices": ["a", "b", "c", "d"], "answer_index": 0, "evidence": ""},
        ], ensure_ascii=False)
        result = services._parse_mcq_json(raw)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["text"], "보기 전부 다름")

    def test_parse_mcq_json_extracts_array_even_with_surrounding_prose(self):
        raw = "물론입니다! 요청하신 문제는 다음과 같습니다:\n```json\n%s\n```\n참고하세요." % self._mcq_json(1)
        result = services._parse_mcq_json(raw)
        self.assertEqual(len(result), 1)

    def test_parse_mcq_json_drops_items_with_hallucinated_evidence(self):
        raw = json.dumps([
            {"question": "진짜 근거", "choices": ["a", "b", "c", "d"], "answer_index": 0, "evidence": "A안으로 진행하기로"},
            {"question": "지어낸 근거", "choices": ["a", "b", "c", "d"], "answer_index": 0, "evidence": "전사본에 없는 문장"},
        ], ensure_ascii=False)
        result = services._parse_mcq_json(raw, source_text="오늘 회의에서 A안으로 진행하기로 결정했습니다.")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["text"], "진짜 근거")

    def test_generate_check_questions_uses_transcript_not_record_as_source(self):
        with patch.dict(os.environ, {"OLLAMA_MODEL": "m1"}), patch.object(services, "_generate", return_value=self._mcq_json(1)) as gen:
            services.generate_check_questions(self.meeting)
        self.assertIn(self.meeting.transcript, gen.call_args[0][0])


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

    def test_check_questions_rejects_mixed_language_output(self):
        garbled = "IPv6的地址空间更大吗？这是一个问题です"
        with patch.dict(os.environ, {"OLLAMA_MODEL": "m1"}), patch.object(services, "_generate", return_value=garbled):
            self.assertEqual(services.generate_check_questions(self.meeting), services.FAKE_CHECK_QUESTIONS)

    def test_summarize_rejects_mixed_language_output(self):
        with patch.dict(os.environ, {"OLLAMA_MODEL": "m1"}), patch.object(services, "_generate", return_value="这是总结"):
            self.assertEqual(services.summarize("원문"), "원문")

    def test_chat_reply_rejects_mixed_language_output(self):
        with patch.dict(os.environ, {"OLLAMA_MODEL": "m1"}), patch.object(services, "_generate", return_value="这是回答"):
            self.assertIn("언어 오류", services.chat_reply(self.meeting, "q"))

    def test_retries_once_then_succeeds_on_clean_output(self):
        clean = json.dumps([{"question": "질문1", "choices": ["a", "b", "c", "d"], "answer_index": 0, "evidence": ""}], ensure_ascii=False)
        with patch.dict(os.environ, {"OLLAMA_MODEL": "m1"}), patch.object(
            services, "_generate", side_effect=["这是中文", clean]
        ) as gen:
            result = services.generate_check_questions(self.meeting)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["text"], "질문1")
        self.assertEqual(gen.call_count, 2)

    def test_gives_up_after_one_retry_still_garbled(self):
        with patch.dict(os.environ, {"OLLAMA_MODEL": "m1"}), patch.object(
            services, "_generate", return_value="这是中文"
        ) as gen:
            self.assertEqual(services.generate_check_questions(self.meeting), services.FAKE_CHECK_QUESTIONS)
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
            self.assertEqual(services.stt_status_text(), "전사 엔진: 로컬 모델(faster-whisper)")
        SttUsage.objects.create(audio_seconds=750)
        with patch.dict(os.environ, {**self.CLOVA_ENV, **self.LOCAL_ENV}, clear=True):
            self.assertEqual(
                services.stt_status_text(),
                "전사 엔진: CLOVA Speech · 이번 달 무료 사용량 12.5/20분 (소진 시 로컬 모델(faster-whisper)로 전환)",
            )
        with patch.dict(os.environ, {**self.CLOVA_ENV, "CLOVA_SPEECH_FREE_MINUTES": "60"}, clear=True):
            self.assertEqual(services.stt_status_text(), "전사 엔진: CLOVA Speech · 이번 달 무료 사용량 12.5/60분")
        SttUsage.objects.create(audio_seconds=450)
        with patch.dict(os.environ, self.CLOVA_ENV, clear=True):
            self.assertIn("20/20분 (소진 — 로컬 모델이 없어 전사가 실패합니다)", services.stt_status_text())
        with patch.dict(os.environ, {**self.CLOVA_ENV, **self.LOCAL_ENV}, clear=True):
            self.assertIn("(소진 → 로컬 모델(faster-whisper)로 전사)", services.stt_status_text())

    def test_meeting_detail_shows_stt_status_only_when_configured(self):
        url = reverse("meeting_detail", args=[self.meeting.id])
        with patch.dict(os.environ, {}, clear=True):
            self.assertNotContains(self.client.get(url), "전사 엔진")
        with patch.dict(os.environ, self.CLOVA_ENV, clear=True):
            self.assertContains(self.client.get(url), "전사 엔진: CLOVA Speech · 이번 달 무료 사용량 0/20분")


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
            self.assertEqual(services.stt_status_text(), "전사 엔진: Groq Whisper")


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
        self.meeting.transcript = "전사 원문 내용"
        self.meeting.save()
        r = self.client.get(reverse("summary", args=[self.meeting.id]))
        self.assertContains(r, "전사 원문 보기")
        self.assertContains(r, "전사 원문 내용")


class DiscrepancyTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("a", password="pw12345!")
        self.other = User.objects.create_user("b", password="pw12345!")
        self.client.force_login(self.user)
        self.team = Team.objects.create(name="t")
        self.team.members.add(self.user, self.other)
        self.meeting = Meeting.objects.create(team=self.team, title="m", host=self.user, record="결정: 로그인은 이메일만")
        self.question = CheckQuestion.objects.create(
            meeting=self.meeting, text="로그인 범위는?",
            choices=["이메일만", "이메일+소셜", "소셜만", "모름"], correct_choice=0,
            evidence="로그인은 이메일만 지원한다", status=CheckQuestion.STATUS_PUBLISHED,
        )

    def _answer(self, a, b):
        Answer.objects.create(question=self.question, user=self.user, selected_choice=a)
        Answer.objects.create(question=self.question, user=self.other, selected_choice=b)

    def test_identical_correct_answers_show_no_discrepancy(self):
        self._answer(0, 0)
        r = self.client.get(reverse("verification", args=[self.meeting.id]))
        self.assertContains(r, "엇갈린 답변이 없습니다.")

    def test_wrong_answer_flagged_as_discrepancy(self):
        self._answer(0, 1)
        r = self.client.get(reverse("verification", args=[self.meeting.id]))
        self.assertNotContains(r, "엇갈린 답변이 없습니다.")
        self.assertContains(r, "a: 이메일만")
        self.assertContains(r, "b: 이메일+소셜")

    def test_progress_bar_shows_answer_completion(self):
        Answer.objects.create(question=self.question, user=self.user, selected_choice=0)
        r = self.client.get(reverse("verification", args=[self.meeting.id]))
        self.assertEqual(r.context["answered_slots"], 1)
        self.assertEqual(r.context["total_slots"], 2)
        self.assertEqual(r.context["progress_pct"], 50)
        self.assertContains(r, "1 / 2")

    def test_view_shows_evidence_for_discrepancy(self):
        self._answer(0, 1)
        r = self.client.get(reverse("verification", args=[self.meeting.id]))
        self.assertContains(r, "로그인은 이메일만 지원한다")

    def test_draft_questions_are_not_counted_in_verification(self):
        self.question.status = CheckQuestion.STATUS_DRAFT
        self.question.save()
        r = self.client.get(reverse("verification", args=[self.meeting.id]))
        self.assertEqual(r.context["total_slots"], 0)


class QuestionTimingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("a", password="pw12345!")
        self.client.force_login(self.user)
        self.team = Team.objects.create(name="t")
        self.team.members.add(self.user)
        self.meeting = Meeting.objects.create(team=self.team, title="m", host=self.user)

    def test_no_questions_are_created_before_transcript_exists(self):
        r = self.client.get(reverse("question_review", args=[self.meeting.id]))
        self.assertEqual(CheckQuestion.objects.filter(meeting=self.meeting).count(), 0)
        self.assertContains(r, "아직 문제가 없습니다")

    def test_questions_are_created_once_transcript_exists(self):
        self.meeting.transcript = "오늘 회의에서 A안으로 진행하기로 결정했습니다."
        self.meeting.save()
        raw = json.dumps([
            {"question": "질문1", "choices": ["a", "b", "c", "d"], "answer_index": 0, "evidence": ""},
            {"question": "질문2", "choices": ["a", "b", "c", "d"], "answer_index": 1, "evidence": ""},
        ], ensure_ascii=False)
        with patch.dict(os.environ, {"OLLAMA_MODEL": "m1"}), patch.object(services, "_generate", return_value=raw):
            self.client.get(reverse("question_review", args=[self.meeting.id]))
            self.client.get(reverse("question_review", args=[self.meeting.id]))
        self.assertEqual(list(CheckQuestion.objects.filter(meeting=self.meeting).values_list("text", flat=True)), ["질문1", "질문2"])

    def test_no_new_transcript_banner_right_after_initial_generation(self):
        self.meeting.transcript = "오늘 회의에서 A안으로 진행하기로 결정했습니다."
        self.meeting.save()
        raw = json.dumps([{"question": "질문1", "choices": ["a", "b", "c", "d"], "answer_index": 0, "evidence": ""}], ensure_ascii=False)
        with patch.dict(os.environ, {"OLLAMA_MODEL": "m1"}), patch.object(services, "_generate", return_value=raw):
            self.client.get(reverse("question_review", args=[self.meeting.id]))
        r = self.client.get(reverse("question_review", args=[self.meeting.id]))
        self.assertNotContains(r, "추가 질문 생성")

    def test_new_transcript_after_additional_recording_shows_banner(self):
        self.meeting.transcript = "오늘 회의에서 A안으로 진행하기로 결정했습니다."
        self.meeting.save()
        raw = json.dumps([{"question": "질문1", "choices": ["a", "b", "c", "d"], "answer_index": 0, "evidence": ""}], ensure_ascii=False)
        with patch.dict(os.environ, {"OLLAMA_MODEL": "m1"}), patch.object(services, "_generate", return_value=raw):
            self.client.get(reverse("question_review", args=[self.meeting.id]))
        self.meeting.refresh_from_db()
        self.meeting.transcript += " 추가로 녹음된 내용입니다."
        self.meeting.save()
        r = self.client.get(reverse("question_review", args=[self.meeting.id]))
        self.assertContains(r, "추가 질문 생성")

    def test_generate_more_only_passes_new_transcript_portion(self):
        self.meeting.transcript = "원본 내용입니다."
        self.meeting.check_questions_transcript = "원본 내용입니다."
        self.meeting.save()
        CheckQuestion.objects.create(
            meeting=self.meeting, text="기존질문", choices=["a", "b", "c", "d"], correct_choice=0, batch=0, order=0,
        )
        self.meeting.transcript = "원본 내용입니다. 추가된 내용입니다."
        self.meeting.save()
        with patch.object(services, "generate_check_questions_from_text", return_value=[]) as gen:
            self.client.post(reverse("question_review", args=[self.meeting.id]), {"action": "generate_more"})
        gen.assert_called_once_with("추가된 내용입니다.")

    def test_generate_more_creates_new_batch_without_touching_existing(self):
        self.meeting.transcript = "원본 내용입니다."
        self.meeting.save()
        raw1 = json.dumps([{"question": "질문1", "choices": ["a", "b", "c", "d"], "answer_index": 0, "evidence": ""}], ensure_ascii=False)
        with patch.dict(os.environ, {"OLLAMA_MODEL": "m1"}), patch.object(services, "_generate", return_value=raw1):
            self.client.get(reverse("question_review", args=[self.meeting.id]))

        self.meeting.refresh_from_db()
        self.meeting.transcript += " 추가 녹음 내용입니다."
        self.meeting.save()

        raw2 = json.dumps([{"question": "질문2", "choices": ["a", "b", "c", "d"], "answer_index": 1, "evidence": ""}], ensure_ascii=False)
        with patch.dict(os.environ, {"OLLAMA_MODEL": "m1"}), patch.object(services, "_generate", return_value=raw2):
            self.client.post(reverse("question_review", args=[self.meeting.id]), {"action": "generate_more"})

        q1 = CheckQuestion.objects.get(text="질문1")
        q2 = CheckQuestion.objects.get(text="질문2")
        self.assertEqual(q1.batch, 0)
        self.assertEqual(q2.batch, 1)

    def test_understanding_page_groups_questions_by_batch(self):
        self.meeting.transcript = "원본 내용입니다."
        self.meeting.save()
        CheckQuestion.objects.create(
            meeting=self.meeting, text="최초질문", choices=["a", "b", "c", "d"], correct_choice=0,
            batch=0, order=0, status=CheckQuestion.STATUS_PUBLISHED,
        )
        CheckQuestion.objects.create(
            meeting=self.meeting, text="추가질문", choices=["a", "b", "c", "d"], correct_choice=0,
            batch=1, order=0, status=CheckQuestion.STATUS_PUBLISHED,
        )
        r = self.client.get(reverse("understanding", args=[self.meeting.id]))
        self.assertContains(r, "추가 질문 1차")


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(), BACKGROUND_RECORDING=False)
class BackgroundProcessingTests(TestCase):
    def setUp(self):
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
        self.assertContains(r, "녹음을 전사하고")

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
            self.assertIn("LLM 연결 실패", services.chat_reply(self.meeting, "q"))
            self.assertEqual(services.generate_check_questions(self.meeting), services.FAKE_CHECK_QUESTIONS)

    def test_gemini_retries_transient_error_then_succeeds(self):
        import urllib.error
        payload = {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}
        success = self._fake_gemini_urlopen({}, payload)
        calls = []

        def flaky_urlopen(request, timeout):
            calls.append(timeout)
            if len(calls) < 3:
                raise urllib.error.HTTPError("u", 503, "Service Unavailable", {}, None)
            return success(request, timeout)

        with patch.dict(os.environ, {"GEMINI_API_KEY": "k1", "GEMINI_MODEL": "gemini-x"}, clear=True), \
                patch("urllib.request.urlopen", side_effect=flaky_urlopen), patch.object(services.time, "sleep") as sleep:
            self.assertEqual(services._generate("hi"), "ok")
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(calls[0], services.GEMINI_DEFAULT_TIMEOUT_SECONDS)

    def test_gemini_does_not_retry_auth_error_or_timeout(self):
        import urllib.error
        for error in (urllib.error.HTTPError("u", 401, "Unauthorized", {}, None), TimeoutError("timed out")):
            with patch.dict(os.environ, {"GEMINI_API_KEY": "k1", "GEMINI_MODEL": "gemini-x"}, clear=True), \
                    patch("urllib.request.urlopen", side_effect=error) as urlopen, patch.object(services.time, "sleep") as sleep:
                self.assertRaises(OSError, services._generate, "hi")
            self.assertEqual(urlopen.call_count, 1)
            sleep.assert_not_called()

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
                patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")) as urlopen:
            self.assertRaises(OSError, services._generate, "hi")
        self.assertEqual(urlopen.call_count, 1)

    def test_gemini_timeout_env_overrides_default(self):
        captured = {}
        payload = {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}
        fake = self._fake_gemini_urlopen(captured, payload)
        with patch.dict(os.environ, {"GEMINI_API_KEY": "k1", "GEMINI_MODEL": "gemini-x", "GEMINI_TIMEOUT": "42"}, clear=True), \
                patch("urllib.request.urlopen", side_effect=fake) as urlopen:
            services._generate("hi")
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 42)


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

    def test_verification_page_is_separate_from_answer_page(self):
        r = self.client.get(reverse("understanding", args=[self.meeting.id]))
        self.assertNotContains(r, "엇갈린 답변이 없습니다.")
        self.assertContains(r, reverse("verification", args=[self.meeting.id]))
        self.assertEqual(self.client.get(reverse("verification", args=[self.meeting.id])).status_code, 200)

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

    def test_duplicate_check_question_order_is_rejected_by_db(self):
        CheckQuestion.objects.create(meeting=self.meeting, text="q1", order=0)
        with self.assertRaises(IntegrityError), transaction.atomic():
            CheckQuestion.objects.create(meeting=self.meeting, text="q2", order=0)

    def test_question_review_does_not_duplicate_existing_questions(self):
        CheckQuestion.objects.create(meeting=self.meeting, text="기존", order=0)
        raw = json.dumps([{"question": "새1", "choices": ["a", "b", "c", "d"], "answer_index": 0, "evidence": ""}], ensure_ascii=False)
        with patch.dict(os.environ, {"OLLAMA_MODEL": "m1"}), patch.object(services, "_generate", return_value=raw):
            self.client.get(reverse("question_review", args=[self.meeting.id]))
        self.assertEqual(CheckQuestion.objects.filter(meeting=self.meeting, order=0).count(), 1)

    def _published_question(self, text="Q", order=0):
        return CheckQuestion.objects.create(
            meeting=self.meeting, text=text, order=order,
            choices=["a", "b", "c", "d"], correct_choice=0, status=CheckQuestion.STATUS_PUBLISHED,
        )

    def test_verification_lists_members_who_have_not_answered(self):
        question = self._published_question()
        Answer.objects.create(question=question, user=self.user, selected_choice=0)
        r = self.client.get(reverse("verification", args=[self.meeting.id]))
        self.assertContains(r, "아직 답하지 않은 팀원")
        self.assertContains(r, "b")

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


class QuizPendingTests(TestCase):
    """확인 퀴즈 미완료 인원 배지(팀 홈, 숫자만)와 미완료 참가자 명단(진행자 전용)."""

    def setUp(self):
        self.host = User.objects.create_user("quizhost", password="pw12345!")
        self.alpha = User.objects.create_user("memb-alpha", password="pw12345!")
        self.beta = User.objects.create_user("memb-beta", password="pw12345!")
        self.gamma = User.objects.create_user("memb-gamma", password="pw12345!")
        self.team = Team.objects.create(name="t", created_by=self.host)
        self.team.members.add(self.host, self.alpha, self.beta, self.gamma)
        self.meeting = Meeting.objects.create(
            team=self.team, title="m", host=self.host, record="회의록", transcript="전사본",
        )
        self.q1 = self._question(order=0)
        self.q2 = self._question(order=1)
        self.client.force_login(self.host)

    def _question(self, order, batch=0, status=CheckQuestion.STATUS_PUBLISHED):
        return CheckQuestion.objects.create(
            meeting=self.meeting, text="문항 %d-%d" % (batch, order),
            choices=["보기1", "보기2", "보기3", "보기4"], correct_choice=0,
            evidence="근거", status=status, order=order, batch=batch,
        )

    def _solve(self, user, questions, selected=0):
        for question in questions:
            Answer.objects.update_or_create(question=question, user=user, defaults={"selected_choice": selected})

    def _home(self, user=None):
        client = self.client
        if user is not None:
            client = Client()
            client.force_login(user)
        return client.get(reverse("meeting_list", args=[self.team.id]))

    def _review(self):
        return self.client.get(reverse("question_review", args=[self.meeting.id]))

    def test_unpublished_quiz_shows_no_badge_and_no_host_section(self):
        self.meeting.check_questions.update(status=CheckQuestion.STATUS_DRAFT)
        home = self._home()
        self.assertIsNone(home.context["rows"][0]["pending"])
        self.assertNotContains(home, 'class="pending-badge"')
        self.assertNotContains(home, "모두 완료")
        review = self._review()
        self.assertFalse(review.context["quiz_open"])
        self.assertNotContains(review, "아직 풀지 않은 참가자")

    def test_badge_counts_participants_who_have_not_finished(self):
        self._solve(self.alpha, [self.q1, self.q2])
        home = self._home()
        self.assertEqual(home.context["rows"][0]["pending"], 2)
        self.assertContains(home, 'class="pending-badge"')
        self.assertContains(home, ">2</span>")
        self.assertContains(home, "아직 2명이 확인 퀴즈를 풀지 않았어요")
        review = self._review()
        self.assertContains(review, "3명 중 1명 완료")
        self.assertContains(review, "memb-beta")
        self.assertContains(review, "memb-gamma")
        self.assertNotContains(review, "memb-alpha")

    def test_badge_shows_99_plus_over_limit(self):
        self.assertEqual(quiz_status.badge_text(99), "99")
        self.assertEqual(quiz_status.badge_text(100), "99+")

    def test_all_finished_shows_completion_markers(self):
        for member in (self.alpha, self.beta, self.gamma):
            self._solve(member, [self.q1, self.q2])
        home = self._home()
        self.assertEqual(home.context["rows"][0]["pending"], 0)
        self.assertNotContains(home, 'class="pending-badge"')
        self.assertContains(home, "모두 완료")
        review = self._review()
        self.assertContains(review, "3명 중 3명 완료")
        self.assertContains(review, "모든 참가자가 퀴즈를 마쳤어요")

    def test_host_is_excluded_from_the_count(self):
        for member in (self.alpha, self.beta, self.gamma):
            self._solve(member, [self.q1, self.q2])
        # 진행자는 한 문항도 풀지 않았지만 독려 대상이 아니라서 집계에서 빠진다.
        self.assertFalse(Answer.objects.filter(user=self.host).exists())
        self.assertEqual(quiz_status.participant_count(self.meeting), 3)
        self.assertEqual(self._home().context["rows"][0]["pending"], 0)
        self.assertEqual(self._review().context["pending_rows"], [])

    def test_newly_published_question_makes_finished_participant_pending_again(self):
        for member in (self.alpha, self.beta, self.gamma):
            self._solve(member, [self.q1, self.q2])
        self.assertEqual(self._home().context["rows"][0]["pending"], 0)
        # 녹음을 추가로 올려 다음 batch 문항이 공개된 상황 (PR #31)
        extra = self._question(order=0, batch=1)
        self.assertEqual(self._home().context["rows"][0]["pending"], 3)
        self._solve(self.alpha, [extra])
        self.assertEqual(self._home().context["rows"][0]["pending"], 2)
        review = self._review()
        self.assertContains(review, "2/3문항")
        self.assertNotContains(review, "memb-alpha")

    def test_participant_pages_contain_no_other_participant_names(self):
        self._solve(self.alpha, [self.q1])
        client = Client()
        client.force_login(self.alpha)
        for url in (
            reverse("meeting_list", args=[self.team.id]),
            reverse("meeting_detail", args=[self.meeting.id]),
            reverse("understanding", args=[self.meeting.id]),
        ):
            response = client.get(url)
            self.assertEqual(response.status_code, 200)
            body = response.content.decode()
            for name in ("memb-beta", "memb-gamma", "quizhost"):
                self.assertNotIn(name, body, "%s 응답에 %s가 노출됨" % (url, name))

    def test_participant_gets_403_on_host_screen(self):
        client = Client()
        client.force_login(self.alpha)
        self.assertEqual(client.get(reverse("question_review", args=[self.meeting.id])).status_code, 403)

    def test_host_screen_does_not_reveal_who_chose_what(self):
        # 전부 오답으로 풀었어도 '완료'라서 명단에 안 나온다 — 이름은 완료 여부에만 연결된다.
        self._solve(self.alpha, [self.q1, self.q2], selected=1)
        review = self._review()
        self.assertNotContains(review, "memb-alpha")
        self.assertNotContains(review, "오답")
        self.assertContains(review, "memb-beta")

    def test_meeting_list_query_count_does_not_grow_with_meetings(self):
        self._solve(self.alpha, [self.q1])
        url = reverse("meeting_list", args=[self.team.id])
        with CaptureQueriesContext(connection) as one_meeting:
            self.assertEqual(self.client.get(url).status_code, 200)
        for i in range(4):
            extra = Meeting.objects.create(team=self.team, title="m%d" % i, host=self.host, transcript="전사본")
            CheckQuestion.objects.create(
                meeting=extra, text="q", choices=["1", "2", "3", "4"],
                correct_choice=0, status=CheckQuestion.STATUS_PUBLISHED,
            )
        with CaptureQueriesContext(connection) as five_meetings:
            response = self.client.get(url)
        self.assertEqual(len(response.context["rows"]), 5)
        self.assertEqual(len(five_meetings.captured_queries), len(one_meeting.captured_queries))


class PartialAnswerNoticeTests(TestCase):
    """부분 응답은 막지 않되, 몇 문항이 남았는지 당사자 화면에 알려준다."""

    def setUp(self):
        self.host = User.objects.create_user("quizhost", password="pw12345!")
        self.member = User.objects.create_user("memb-alpha", password="pw12345!")
        self.team = Team.objects.create(name="t", created_by=self.host)
        self.team.members.add(self.host, self.member)
        self.meeting = Meeting.objects.create(team=self.team, title="m", host=self.host, transcript="전사본")
        self.questions = [
            CheckQuestion.objects.create(
                meeting=self.meeting, text="문항 %d" % order,
                choices=["보기1", "보기2", "보기3", "보기4"], correct_choice=0,
                status=CheckQuestion.STATUS_PUBLISHED, order=order,
            )
            for order in range(3)
        ]
        self.client.force_login(self.member)
        self.url = reverse("understanding", args=[self.meeting.id])

    def test_first_visit_shows_progress_but_no_unanswered_badges(self):
        r = self.client.get(self.url)
        self.assertContains(r, "3문항 중 0문항 답함")
        # 아직 한 번도 답하지 않았으면 문항마다 '미응답'을 달지 않는다(시끄럽기만 함).
        self.assertNotContains(r, "미응답")
        self.assertNotContains(r, "남았습니다")

    def test_partial_submit_warns_how_many_are_left(self):
        # 3문항 중 1문항만 고르고 저장 — 뷰가 빈 문항을 건너뛰므로 그대로 저장된다.
        self.client.post(self.url, {"q%d" % self.questions[0].id: "0"})
        r = self.client.get(self.url)
        self.assertContains(r, "3문항 중 1문항 답함")
        self.assertContains(r, "아직 2문항이 남았습니다")
        self.assertContains(r, "미응답")
        self.assertEqual(r.context["remaining_count"], 2)
        self.assertEqual(r.context["progress_pct"], 33)

    def test_all_answered_shows_completion_line(self):
        self.client.post(self.url, {"q%d" % q.id: "0" for q in self.questions})
        r = self.client.get(self.url)
        self.assertContains(r, "3문항 모두 답했습니다")
        self.assertNotContains(r, "남았습니다")
        self.assertNotContains(r, "미응답")
        self.assertEqual(r.context["progress_pct"], 100)

    def test_newly_published_batch_reopens_the_notice(self):
        self.client.post(self.url, {"q%d" % q.id: "0" for q in self.questions})
        self.assertContains(self.client.get(self.url), "3문항 모두 답했습니다")
        # 녹음을 추가해 batch 1 문항이 공개되면, 잘못한 것 없이도 다시 미완료가 된다.
        CheckQuestion.objects.create(
            meeting=self.meeting, text="추가 문항", choices=["1", "2", "3", "4"],
            correct_choice=0, status=CheckQuestion.STATUS_PUBLISHED, order=0, batch=1,
        )
        r = self.client.get(self.url)
        self.assertContains(r, "4문항 중 3문항 답함")
        self.assertContains(r, "아직 1문항이 남았습니다")


def _spec(who="민수", what="배포 준비", due="금요일", done="스테이징 확인", state=Decision.CONFIRM_CONFIRMED):
    return {"who": who, "what": what, "due": due, "done": done, "state": state}


class DecisionWarningTests(TestCase):
    """① 컴파일의 경고 판정. LLM이 아니라 decision_compiler의 규칙으로 세므로 DB도 LLM도 필요 없다."""

    def test_blank_fields_each_raise_their_own_warning(self):
        warnings = decision_compiler.warnings_for([_spec(who="", due="", done="")])[0]
        self.assertEqual(warnings, ["담당자 빈칸", "기한 빈칸", "완료 기준 빈칸"])

    def test_fully_filled_spec_has_no_warning(self):
        self.assertEqual(decision_compiler.warnings_for([_spec()])[0], [])

    def test_each_ambiguous_phrase_is_detected(self):
        for phrase in decision_compiler.AMBIGUOUS_PHRASES:
            warnings = decision_compiler.warnings_for([_spec(due="%s 끝내기" % phrase)])[0]
            self.assertIn("모호어 '%s'" % phrase, warnings, phrase)

    def test_two_dates_in_one_spec_raise_a_conflict_warning(self):
        warnings = decision_compiler.warnings_for([_spec(due="금요일 아니면 다음 주 월요일")])[0]
        self.assertTrue(any(w.startswith("같은 항목에 다른 날짜") for w in warnings), warnings)

    def test_a_single_date_written_as_month_and_day_is_not_a_conflict(self):
        # "12월 5일"을 "12월"과 "5일"로 쪼개 세면 날짜가 하나인 명세에도 경고가 붙는다.
        warnings = decision_compiler.warnings_for([_spec(due="12월 5일")])[0]
        self.assertFalse(any(w.startswith("같은 항목에 다른 날짜") for w in warnings), warnings)

    def test_two_quantities_in_one_spec_raise_a_conflict_warning(self):
        warnings = decision_compiler.warnings_for([_spec(what="테스트 3개 작성", done="5개 통과")])[0]
        self.assertTrue(any(w.startswith("같은 항목에 다른 숫자") for w in warnings), warnings)

    def test_same_owner_with_different_due_dates_warns_on_both_items(self):
        rows = decision_compiler.warnings_for([
            _spec(who="민수", what="배포", due="금요일"),
            _spec(who="민수", what="문서", due="다음 주 화요일"),
        ])
        for warnings in rows:
            self.assertTrue(any("'민수'에게 다른 날짜" in w for w in warnings), warnings)

    def test_same_owner_with_the_same_due_date_does_not_warn(self):
        rows = decision_compiler.warnings_for([
            _spec(who="민수", what="배포", due="금요일"),
            _spec(who="민수", what="문서", due="금요일"),
        ])
        self.assertEqual(rows, [[], []])

    def test_different_owners_are_not_compared_with_each_other(self):
        rows = decision_compiler.warnings_for([
            _spec(who="민수", due="금요일"),
            _spec(who="영희", due="다음 주 화요일"),
        ])
        self.assertEqual(rows, [[], []])

    def test_at_most_five_items_keep_their_warnings(self):
        specs = [_spec(what="할 일 %d" % i, done="") for i in range(7)]
        rows = decision_compiler.warnings_for(specs)
        self.assertEqual(sum(1 for warnings in rows if warnings), decision_compiler.MAX_WARNED_DECISIONS)
        # 잘릴 때는 뒤쪽(전사본에서 나중에 나온) 항목의 경고를 비운다.
        self.assertTrue(all(rows[i] for i in range(5)))
        self.assertEqual(rows[5], [])
        self.assertEqual(rows[6], [])

    def test_items_without_warnings_do_not_use_up_the_five_slots(self):
        specs = [_spec(done="") for _ in range(3)] + [_spec() for _ in range(4)] + [_spec(done="") for _ in range(3)]
        rows = decision_compiler.warnings_for(specs)
        self.assertEqual(sum(1 for warnings in rows if warnings), 5)

    def test_the_same_warning_reason_is_not_listed_twice(self):
        warnings = decision_compiler.warnings_for([_spec(who="각자", what="각자 정리", due="", done="")])[0]
        self.assertEqual(warnings.count("모호어 '각자'"), 1)


class DecisionParseTests(TestCase):
    """LLM 응답의 JSON 스키마 검증과 근거 검증."""

    def test_parse_decisions_reads_the_documented_schema(self):
        raw = json.dumps([{"who": "민수", "what": "배포", "due": "금요일", "done": "확인", "state": "확정"}], ensure_ascii=False)
        specs = decision_compiler.parse_decisions(raw)
        self.assertEqual(specs, [{
            "who": "민수", "what": "배포", "due": "금요일", "done": "확인",
            "state": Decision.CONFIRM_CONFIRMED,
        }])

    def test_parse_decisions_survives_code_fences_and_prose(self):
        raw = "네, 정리했습니다.\n```json\n[{\"what\": \"배포\"}]\n```\n이상입니다."
        self.assertEqual(len(decision_compiler.parse_decisions(raw)), 1)

    def test_parse_decisions_drops_items_without_what(self):
        raw = json.dumps([{"who": "민수", "what": "", "due": "금요일"}, {"what": "배포"}], ensure_ascii=False)
        specs = decision_compiler.parse_decisions(raw)
        self.assertEqual([spec["what"] for spec in specs], ["배포"])

    def test_parse_decisions_keeps_blank_who_and_due(self):
        # 빈칸은 버릴 흠이 아니라 경고의 재료이므로 그대로 남아야 한다.
        specs = decision_compiler.parse_decisions(json.dumps([{"what": "배포"}]))
        self.assertEqual(specs[0]["who"], "")
        self.assertEqual(specs[0]["due"], "")

    def test_parse_decisions_defaults_unknown_state_to_unset(self):
        specs = decision_compiler.parse_decisions(json.dumps([{"what": "배포", "state": "아마도"}], ensure_ascii=False))
        self.assertEqual(specs[0]["state"], Decision.CONFIRM_UNSET)

    def test_parse_decisions_returns_empty_on_broken_json(self):
        self.assertEqual(decision_compiler.parse_decisions("JSON 못 만들겠습니다"), [])
        self.assertEqual(decision_compiler.parse_decisions('[{"what": "배포",]'), [])
        self.assertEqual(decision_compiler.parse_decisions('{"what": "배포"}'), [])

    def test_parse_decisions_caps_the_number_of_items(self):
        raw = json.dumps([{"what": "할 일 %d" % i} for i in range(30)], ensure_ascii=False)
        self.assertEqual(len(decision_compiler.parse_decisions(raw)), decision_compiler.MAX_DECISIONS)

    def test_interpretation_with_evidence_missing_from_the_transcript_is_dropped(self):
        source = "민수가 금요일까지 배포를 맡기로 했습니다."
        raw = json.dumps([
            {"text": "금요일 퇴근 전까지라는 뜻", "evidence": "민수가 금요일까지 배포를 맡기로 했습니다."},
            {"text": "다음 주 금요일이라는 뜻", "evidence": "다음 주 금요일까지라고 했습니다."},
        ], ensure_ascii=False)
        results = decision_compiler.parse_interpretations(raw, source_text=source)
        self.assertEqual([r["text"] for r in results], ["금요일 퇴근 전까지라는 뜻"])

    def test_evidence_matching_ignores_whitespace_differences(self):
        source = "민수가 금요일까지 배포를 맡기로 했습니다."
        raw = json.dumps([{"text": "해석", "evidence": "민수가  금요일까지\n배포를 맡기로"}], ensure_ascii=False)
        self.assertEqual(len(decision_compiler.parse_interpretations(raw, source_text=source)), 1)

    def test_blank_evidence_skips_verification(self):
        raw = json.dumps([{"text": "해석", "evidence": ""}], ensure_ascii=False)
        self.assertEqual(len(decision_compiler.parse_interpretations(raw, source_text="아무 내용")), 1)

    def test_interpretations_without_text_or_duplicated_are_dropped(self):
        raw = json.dumps([{"text": ""}, {"text": "같은 해석"}, {"text": "같은 해석"}], ensure_ascii=False)
        results = decision_compiler.parse_interpretations(raw)
        self.assertEqual([r["text"] for r in results], ["같은 해석"])

    def test_interpretations_are_capped(self):
        raw = json.dumps([{"text": "해석 %d" % i} for i in range(8)], ensure_ascii=False)
        self.assertEqual(len(decision_compiler.parse_interpretations(raw)), decision_compiler.MAX_INTERPRETATIONS)


class DecisionCompileTests(TestCase):
    """①+② 전체. LLM 유무, 파싱 실패 재시도, 가짜 응답 모드를 본다."""

    def setUp(self):
        self.host = User.objects.create_user("host", password="pw12345!")
        self.team = Team.objects.create(name="t")
        self.team.members.add(self.host)
        self.meeting = Meeting.objects.create(
            team=self.team, title="m", host=self.host,
            processing_status=Meeting.STATUS_DONE,
            transcript="민수가 금요일까지 배포를 맡기로 했습니다. 디자인은 각자 보기로 했습니다.",
        )

    def test_fake_mode_runs_the_whole_flow_without_any_llm(self):
        with patch.dict(os.environ, {}, clear=True):
            summary = decision_compiler.compile_meeting(self.meeting)
        self.assertEqual(summary["decisions"], len(decision_compiler.FAKE_DECISIONS))
        self.assertEqual(self.meeting.decisions.count(), len(decision_compiler.FAKE_DECISIONS))
        # 가짜 명세 3개 중 완전히 채워진 1개는 경고가 없고, 나머지 2개는 해석 후보까지 생긴다.
        self.assertEqual(summary["warned"], 2)
        self.assertEqual(summary["unvotable"], 0)
        self.assertEqual(Interpretation.objects.filter(decision__meeting=self.meeting).count(), 4)
        self.assertEqual(len(decision_compiler.votable_decisions(self.meeting)), 2)

    def test_items_without_warnings_get_no_interpretations(self):
        with patch.dict(os.environ, {}, clear=True):
            decision_compiler.compile_meeting(self.meeting)
        for decision in self.meeting.decisions.all():
            if decision.warnings:
                self.assertGreater(decision.interpretations.count(), 0, decision.spec_line)
            else:
                self.assertEqual(decision.interpretations.count(), 0, decision.spec_line)

    def _llm(self, *responses):
        return patch.object(services, "_generate", side_effect=list(responses))

    def test_compile_uses_the_llm_response_when_a_model_is_configured(self):
        compiled = json.dumps([{"who": "민수", "what": "배포", "due": "금요일", "done": "", "state": "확정"}], ensure_ascii=False)
        interpretations = json.dumps([
            {"text": "이번 주 금요일이라는 뜻", "evidence": "민수가 금요일까지 배포를 맡기로 했습니다."},
            {"text": "다음 주 금요일이라는 뜻", "evidence": "디자인은 각자 보기로 했습니다."},
        ], ensure_ascii=False)
        with patch.dict(os.environ, {"OLLAMA_MODEL": "m1"}), self._llm(compiled, interpretations):
            decision_compiler.compile_meeting(self.meeting)
        decision = self.meeting.decisions.get()
        self.assertEqual(decision.who, "민수")
        self.assertEqual(decision.warnings, ["완료 기준 빈칸"])
        self.assertEqual(decision.interpretations.count(), 2)

    def test_transcript_is_sent_in_the_compile_prompt(self):
        compiled = json.dumps([{"what": "배포", "who": "민수", "due": "금요일", "done": "확인"}], ensure_ascii=False)
        with patch.dict(os.environ, {"OLLAMA_MODEL": "m1"}), self._llm(compiled) as gen:
            decision_compiler.compile_meeting(self.meeting)
        self.assertIn(self.meeting.transcript, gen.call_args_list[0][0][0])

    def test_broken_json_is_retried_once_then_fails(self):
        with patch.dict(os.environ, {"OLLAMA_MODEL": "m1"}), self._llm("못 하겠습니다", "역시 못 하겠습니다") as gen:
            with self.assertRaises(decision_compiler.DecisionCompileError):
                decision_compiler.compile_meeting(self.meeting)
        self.assertEqual(gen.call_count, decision_compiler.PARSE_RETRIES + 1)
        # 실패했으면 반쯤 저장된 명세가 남아 있어서는 안 된다.
        self.assertEqual(self.meeting.decisions.count(), 0)

    def test_second_attempt_succeeding_is_accepted(self):
        good = json.dumps([{"what": "배포", "who": "민수", "due": "금요일", "done": "확인"}], ensure_ascii=False)
        with patch.dict(os.environ, {"OLLAMA_MODEL": "m1"}), self._llm("못 하겠습니다", good) as gen:
            decision_compiler.compile_meeting(self.meeting)
        self.assertEqual(gen.call_count, 2)
        self.assertEqual(self.meeting.decisions.count(), 1)

    def test_connection_failure_falls_back_to_fake_specs(self):
        # 기존 퀴즈 생성과 같은 처리 — 연결 실패는 실패 표시 대신 가짜 응답으로 흐름을 잇는다.
        with patch.dict(os.environ, {"OLLAMA_MODEL": "m1"}), patch.object(services, "_generate", side_effect=OSError("연결 실패")):
            summary = decision_compiler.compile_meeting(self.meeting)
        self.assertEqual(summary["decisions"], len(decision_compiler.FAKE_DECISIONS))

    def test_interpretations_with_fabricated_evidence_leave_the_item_unvotable(self):
        compiled = json.dumps([{"who": "민수", "what": "배포", "due": "금요일", "done": "", "state": "확정"}], ensure_ascii=False)
        # 두 해석 모두 전사 원문에 없는 문장을 근거로 달았으므로 전부 버려진다.
        interpretations = json.dumps([
            {"text": "해석 1", "evidence": "회의에 없던 문장입니다."},
            {"text": "해석 2", "evidence": "이것도 없습니다."},
        ], ensure_ascii=False)
        with patch.dict(os.environ, {"OLLAMA_MODEL": "m1"}), self._llm(compiled, interpretations, interpretations):
            summary = decision_compiler.compile_meeting(self.meeting)
        self.assertEqual(summary["warned"], 1)
        self.assertEqual(summary["unvotable"], 1)
        self.assertEqual(Interpretation.objects.filter(decision__meeting=self.meeting).count(), 0)
        self.assertEqual(decision_compiler.votable_decisions(self.meeting), [])

    def test_recompiling_an_already_compiled_meeting_is_refused(self):
        with patch.dict(os.environ, {}, clear=True):
            decision_compiler.compile_meeting(self.meeting)
            with self.assertRaises(decision_compiler.DecisionCompileError):
                decision_compiler.compile_meeting(self.meeting)

    def test_compile_fails_when_no_decision_can_be_extracted(self):
        with patch.dict(os.environ, {"OLLAMA_MODEL": "m1"}), self._llm("[]", "[]"):
            with self.assertRaises(decision_compiler.DecisionCompileError):
                decision_compiler.compile_meeting(self.meeting)

    def test_dropped_warning_count_is_reported(self):
        specs = [_spec(what="할 일 %d" % i, done="") for i in range(7)]
        with patch.dict(os.environ, {}, clear=True), patch.object(decision_compiler, "compile_specs", return_value=specs):
            summary = decision_compiler.compile_meeting(self.meeting)
        self.assertEqual(summary["decisions"], 7)
        self.assertEqual(summary["warned"], decision_compiler.MAX_WARNED_DECISIONS)
        self.assertEqual(summary["dropped_warnings"], 2)


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
        self.assertContains(r, "결정 컴파일 실행")
        self.assertNotContains(r, "투표 시작")
        self.assertFalse(r.context["compiled"])

    def test_compile_action_saves_specs_and_redirects(self):
        with patch.dict(os.environ, {}, clear=True):
            r = self.client.post(self.url, {"action": "compile"})
        self.assertRedirects(r, self.url)
        self.assertEqual(self.meeting.decisions.count(), len(decision_compiler.FAKE_DECISIONS))
        r = self.client.get(self.url)
        self.assertTrue(r.context["compiled"])
        self.assertEqual(r.context["warned_count"], 2)
        self.assertEqual(r.context["votable_count"], 2)

    def test_compile_failure_is_shown_on_the_page(self):
        with patch.dict(os.environ, {"OLLAMA_MODEL": "m1"}), patch.object(services, "_generate", return_value="안 되겠습니다"):
            r = self.client.post(self.url, {"action": "compile"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "읽지 못했습니다")
        self.assertEqual(self.meeting.decisions.count(), 0)

    def test_editing_a_spec_clears_the_warnings_it_fixes(self):
        self._compile()
        decision = self.meeting.decisions.filter(who="").first()
        self.assertIn("담당자 빈칸", decision.warnings)
        self.client.post(self.url, {
            "action": "save",
            "who_%d" % decision.id: "민수",
            "what_%d" % decision.id: decision.what,
            "due_%d" % decision.id: "금요일",
            "done_%d" % decision.id: "스테이징 배포 확인",
            "confirm_state_%d" % decision.id: Decision.CONFIRM_CONFIRMED,
        })
        decision.refresh_from_db()
        self.assertEqual(decision.who, "민수")
        self.assertEqual(decision.warnings, [])
        self.assertEqual(decision.confirm_state, Decision.CONFIRM_CONFIRMED)

    def test_saving_a_blank_what_leaves_the_item_untouched(self):
        self._compile()
        decision = self.meeting.decisions.first()
        before = decision.what
        self.client.post(self.url, {"action": "save", "what_%d" % decision.id: "   "})
        decision.refresh_from_db()
        self.assertEqual(decision.what, before)

    def test_dropping_interpretations_makes_the_item_unvotable(self):
        self._compile()
        decision = decision_compiler.votable_decisions(self.meeting)[0]
        fields = {"action": "save", "what_%d" % decision.id: decision.what}
        for interpretation in decision.interpretations.all():
            fields["drop_interpretation_%d" % interpretation.id] = "1"
        self.client.post(self.url, fields)
        # decision은 prefetch_related로 받아온 객체라서 .interpretations.count()가 캐시된
        # 옛 값을 돌려준다. 삭제가 실제로 됐는지는 새 쿼리로 확인한다.
        self.assertEqual(Interpretation.objects.filter(decision=decision).count(), 0)
        self.assertNotIn(decision.id, {d.id for d in decision_compiler.votable_decisions(self.meeting)})

    def test_start_vote_moves_only_votable_items_to_voting(self):
        self._compile()
        votable_ids = {d.id for d in decision_compiler.votable_decisions(self.meeting)}
        self.client.post(self.url, {"action": "start_vote"})
        for decision in self.meeting.decisions.all():
            expected = Decision.STATUS_VOTING if decision.id in votable_ids else Decision.STATUS_DRAFT
            self.assertEqual(decision.status, expected, decision.spec_line)

    def test_start_vote_without_any_votable_item_shows_an_error(self):
        self._compile()
        Interpretation.objects.filter(decision__meeting=self.meeting).delete()
        r = self.client.post(self.url, {"action": "start_vote"})
        self.assertContains(r, "투표에 올릴 항목이 없습니다")
        self.assertFalse(self.meeting.decisions.filter(status=Decision.STATUS_VOTING).exists())

    def test_specs_cannot_be_edited_once_voting_started(self):
        self._compile()
        self.client.post(self.url, {"action": "start_vote"})
        decision = self.meeting.decisions.filter(status=Decision.STATUS_VOTING).first()
        before = decision.who
        self.client.post(self.url, {
            "action": "save",
            "who_%d" % decision.id: "바뀐 담당자",
            "what_%d" % decision.id: decision.what,
        })
        decision.refresh_from_db()
        self.assertEqual(decision.who, before)
        self.assertFalse(self.client.get(self.url).context["editable"])

    def test_reset_is_refused_after_voting_started(self):
        self._compile()
        self.client.post(self.url, {"action": "start_vote"})
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


class DecisionEntryLinkTests(TestCase):
    """회의록이 완성된 회의에서만, 진행자에게만 "결정 확인(실험)" 링크가 보인다."""

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
        self.assertContains(r, "결정 확인(실험)")
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

    def test_member_gets_the_vote_screen_link_instead(self):
        self.client.force_login(self.member)
        vote_link = reverse("decision_vote", args=[self.meeting.id])
        for name in ("meeting_result", "meeting_detail"):
            r = self.client.get(reverse(name, args=[self.meeting.id]))
            self.assertContains(r, vote_link, msg_prefix=name)
            self.assertNotContains(r, self.link, msg_prefix=name)


class DecisionVoteTests(TestCase):
    """③ 블라인드 동시 선택. 진행자도 참가자로서 똑같이 고른다."""

    def setUp(self):
        self.host = User.objects.create_user("hostuser", password="pw12345!")
        self.alice = User.objects.create_user("aliceuser", password="pw12345!")
        self.bob = User.objects.create_user("bobuser", password="pw12345!")
        self.outsider = User.objects.create_user("outsideruser", password="pw12345!")
        self.team = Team.objects.create(name="t")
        self.team.members.add(self.host, self.alice, self.bob)
        self.meeting = Meeting.objects.create(
            team=self.team, title="m", host=self.host,
            processing_status=Meeting.STATUS_DONE,
            record="결정", transcript="민수가 금요일까지 배포를 맡기로 했습니다.",
        )
        self.decision = Decision.objects.create(
            meeting=self.meeting, order=0, who="민수", what="배포", due_text="금요일",
            done_criteria="", warnings=["완료 기준 빈칸"], status=Decision.STATUS_VOTING,
        )
        self.a = Interpretation.objects.create(decision=self.decision, order=0, text="이번 주 금요일")
        self.b = Interpretation.objects.create(decision=self.decision, order=1, text="다음 주 금요일")
        self.url = reverse("decision_vote", args=[self.meeting.id])

    def _login(self, user):
        client = Client()
        client.force_login(user)
        return client

    def _vote(self, client, choice, custom_text=""):
        return client.post(self.url, {
            "decision_id": self.decision.id, "choice": choice, "custom_text": custom_text,
        })

    def _interp_choice(self, interpretation):
        return "%s%d" % (decision_compiler.VOTE_CHOICE_PREFIX, interpretation.id)

    # ---- 권한 ----

    def test_outsider_gets_404(self):
        self.assertEqual(self._login(self.outsider).get(self.url).status_code, 404)

    def test_outsider_cannot_vote(self):
        r = self._vote(self._login(self.outsider), self._interp_choice(self.a))
        self.assertEqual(r.status_code, 404)
        self.assertEqual(DecisionVote.objects.count(), 0)

    def test_anonymous_user_is_redirected_to_login(self):
        self.assertEqual(Client().get(self.url).status_code, 302)

    def test_every_team_member_including_the_host_can_vote(self):
        for user in (self.host, self.alice, self.bob):
            client = self._login(user)
            self.assertEqual(client.get(self.url).status_code, 200)
            self.assertEqual(self._vote(client, self._interp_choice(self.a)).status_code, 302)
        self.assertEqual(self.decision.votes.count(), 3)

    def test_meeting_without_a_finished_record_gets_404(self):
        Meeting.objects.filter(pk=self.meeting.pk).update(processing_status=Meeting.STATUS_PROCESSING)
        self.assertEqual(self._login(self.alice).get(self.url).status_code, 404)

    # ---- 블라인드: 내가 고르기 전에는 집계가 응답에 없다 ----

    def test_tally_is_absent_from_the_context_before_i_vote(self):
        self._vote(self._login(self.alice), self._interp_choice(self.a))
        self._vote(self._login(self.bob), self._interp_choice(self.a))
        r = self._login(self.host).get(self.url)
        row = r.context["rows"][0]
        self.assertFalse(row["voted"])
        # 숨기는 게 아니라 키 자체가 없어야 한다 — 템플릿 실수로도 샐 수 없게.
        self.assertNotIn("tally", row)
        self.assertEqual(r.context["tallies"] if "tallies" in r.context else {}, {})

    # 집계 블록에만 나오는 문구. 그냥 "집계"로 찾으면 base.html의 개발 노트 설명에도 걸린다.
    TALLY_MARKER = "명 선택)"

    def test_host_sees_no_tally_before_voting_either(self):
        self._vote(self._login(self.alice), self._interp_choice(self.a))
        r = self._login(self.host).get(self.url)
        self.assertNotContains(r, self.TALLY_MARKER)
        self.assertContains(r, "선택 제출")

    def test_tally_appears_only_after_i_vote(self):
        alice = self._login(self.alice)
        self.assertNotContains(alice.get(self.url), self.TALLY_MARKER)
        self._vote(alice, self._interp_choice(self.a))
        r = alice.get(self.url)
        self.assertContains(r, self.TALLY_MARKER)
        self.assertNotContains(r, "선택 제출")

    def test_tally_is_per_item_not_per_screen(self):
        # 2번 항목은 안 골랐으므로 1번 항목의 집계만 보여야 한다.
        second = Decision.objects.create(
            meeting=self.meeting, order=1, what="문서 정리",
            warnings=["담당자 빈칸"], status=Decision.STATUS_VOTING,
        )
        Interpretation.objects.create(decision=second, order=0, text="해석 A")
        Interpretation.objects.create(decision=second, order=1, text="해석 B")
        alice = self._login(self.alice)
        self._vote(alice, self._interp_choice(self.a))
        rows = {row["decision"].id: row for row in alice.get(self.url).context["rows"]}
        self.assertIn("tally", rows[self.decision.id])
        self.assertNotIn("tally", rows[second.id])

    def test_tally_counts_everyone_once_i_have_voted(self):
        self._vote(self._login(self.alice), self._interp_choice(self.a))
        self._vote(self._login(self.bob), self._interp_choice(self.b))
        self._vote(self._login(self.host), DecisionVote.KIND_NOT_DECIDED)
        tally = self._login(self.host).get(self.url).context["rows"][0]["tally"]
        self.assertEqual(tally["total"], 3)
        self.assertEqual(tally["voted"], 3)
        self.assertEqual(tally["missing"], 0)
        self.assertEqual(tally["not_decided"], 1)
        self.assertEqual([item["count"] for item in tally["interpretations"]], [1, 1])

    def test_tally_reports_how_many_have_not_voted(self):
        alice = self._login(self.alice)
        self._vote(alice, self._interp_choice(self.a))
        tally = alice.get(self.url).context["rows"][0]["tally"]
        self.assertEqual(tally["voted"], 1)
        self.assertEqual(tally["missing"], 2)
        self.assertContains(alice.get(self.url), "2명 미선택")

    # ---- 익명: 이름이 어떤 화면에도 선택과 함께 나오지 않는다 ----

    def test_no_screen_shows_another_persons_name(self):
        """다른 사람의 이름은 어떤 결정 확인 화면에도 나오지 않는다.

        base.html은 상단 칩에 '보는 사람 본인'의 아이디를 늘 띄우므로(기존 동작) 본인
        이름은 제외하고, 나머지 참가자의 이름이 한 번도 안 나오는지를 본다.
        """
        self._vote(self._login(self.alice), self._interp_choice(self.a))
        self._vote(self._login(self.bob), DecisionVote.KIND_CUSTOM, "내가 이해한 내용")
        self._vote(self._login(self.host), self._interp_choice(self.b))
        review_url = reverse("decision_review", args=[self.meeting.id])
        cases = [
            (self.host, self.url),
            (self.host, review_url),
            (self.alice, self.url),
            (self.bob, self.url),
        ]
        for viewer, url in cases:
            page = self._login(viewer).get(url)
            for other in (self.alice, self.bob, self.host):
                if other.id == viewer.id:
                    continue
                self.assertNotContains(page, other.username, msg_prefix="%s/%s" % (viewer.username, url))

    def test_custom_texts_are_not_shown_to_anyone(self):
        self._vote(self._login(self.bob), DecisionVote.KIND_CUSTOM, "밥이 쓴 비밀 해석")
        self._vote(self._login(self.host), self._interp_choice(self.a))
        for client in (self._login(self.host), self._login(self.alice)):
            self.assertNotContains(client.get(self.url), "밥이 쓴 비밀 해석")

    def test_my_own_choice_is_not_echoed_back_on_the_page(self):
        # 본인이 쓴 내용도 다시 띄우지 않는다 — 어깨 너머로 보이면 익명성이 깨지고,
        # 집계는 '어느 보기에 몇 명'까지만 알려주면 충분하다.
        alice = self._login(self.alice)
        self._vote(alice, DecisionVote.KIND_CUSTOM, "내가 쓴 내용")
        r = alice.get(self.url)
        self.assertNotContains(r, "내가 쓴 내용")
        self.assertContains(r, self.TALLY_MARKER)

    # ---- 선택 저장 규칙 ----

    def test_choosing_an_interpretation_is_stored_with_its_id(self):
        self._vote(self._login(self.alice), self._interp_choice(self.b))
        vote = DecisionVote.objects.get(decision=self.decision, user=self.alice)
        self.assertEqual(vote.kind, DecisionVote.KIND_INTERPRETATION)
        self.assertEqual(vote.interpretation_id, self.b.id)
        self.assertEqual(vote.custom_text, "")

    def test_custom_choice_requires_text(self):
        client = self._login(self.alice)
        r = self._vote(client, DecisionVote.KIND_CUSTOM, "   ")
        self.assertContains(r, "한 줄 적어 주세요")
        self.assertEqual(DecisionVote.objects.count(), 0)

    def test_custom_choice_stores_the_text(self):
        self._vote(self._login(self.alice), DecisionVote.KIND_CUSTOM, "  금요일 퇴근 전까지  ")
        vote = DecisionVote.objects.get(user=self.alice)
        self.assertEqual(vote.kind, DecisionVote.KIND_CUSTOM)
        self.assertEqual(vote.custom_text, "금요일 퇴근 전까지")

    def test_not_decided_choice_keeps_no_text(self):
        self._vote(self._login(self.alice), DecisionVote.KIND_NOT_DECIDED, "무시돼야 하는 내용")
        vote = DecisionVote.objects.get(user=self.alice)
        self.assertEqual(vote.kind, DecisionVote.KIND_NOT_DECIDED)
        self.assertEqual(vote.custom_text, "")

    def test_choice_is_required(self):
        r = self._vote(self._login(self.alice), "")
        self.assertContains(r, "하나 골라 주세요")
        self.assertEqual(DecisionVote.objects.count(), 0)

    def test_an_interpretation_from_another_decision_is_rejected(self):
        other = Decision.objects.create(meeting=self.meeting, order=1, what="다른 항목", status=Decision.STATUS_VOTING)
        stranger = Interpretation.objects.create(decision=other, order=0, text="남의 해석")
        r = self._vote(self._login(self.alice), self._interp_choice(stranger))
        self.assertEqual(r.status_code, 404)
        self.assertEqual(DecisionVote.objects.count(), 0)

    def test_voting_on_a_draft_item_is_refused(self):
        Decision.objects.filter(pk=self.decision.pk).update(status=Decision.STATUS_DRAFT)
        r = self._vote(self._login(self.alice), self._interp_choice(self.a))
        self.assertContains(r, "받고 있는 항목이 아닙니다")
        self.assertEqual(DecisionVote.objects.count(), 0)

    def test_a_second_submission_cannot_change_my_choice(self):
        client = self._login(self.alice)
        self._vote(client, self._interp_choice(self.a))
        r = self._vote(client, self._interp_choice(self.b))
        self.assertContains(r, "이미 이 항목에 선택을 제출했습니다")
        vote = DecisionVote.objects.get(user=self.alice)
        self.assertEqual(vote.interpretation_id, self.a.id)
        self.assertEqual(DecisionVote.objects.count(), 1)

    def test_a_decision_from_another_meeting_is_rejected(self):
        other_meeting = Meeting.objects.create(
            team=self.team, title="m2", host=self.host, processing_status=Meeting.STATUS_DONE,
        )
        stranger = Decision.objects.create(
            meeting=other_meeting, order=0, what="남의 회의 항목", status=Decision.STATUS_VOTING,
        )
        r = self._login(self.alice).post(self.url, {
            "decision_id": stranger.id, "choice": DecisionVote.KIND_NOT_DECIDED,
        })
        self.assertEqual(r.status_code, 404)
        self.assertEqual(DecisionVote.objects.count(), 0)

    # ---- 화면 상태 ----

    def test_draft_items_are_not_listed_at_all(self):
        Decision.objects.create(meeting=self.meeting, order=1, what="검토 중인 항목", status=Decision.STATUS_DRAFT)
        r = self._login(self.alice).get(self.url)
        self.assertEqual([row["decision"].id for row in r.context["rows"]], [self.decision.id])
        self.assertNotContains(r, "검토 중인 항목")

    def test_empty_state_before_the_host_starts(self):
        Decision.objects.filter(pk=self.decision.pk).update(status=Decision.STATUS_DRAFT)
        r = self._login(self.alice).get(self.url)
        self.assertContains(r, "아직 결정 확인을 시작하지 않았습니다")

    def test_remaining_count_tracks_my_own_progress(self):
        second = Decision.objects.create(
            meeting=self.meeting, order=1, what="문서", warnings=["담당자 빈칸"], status=Decision.STATUS_VOTING,
        )
        Interpretation.objects.create(decision=second, order=0, text="해석 A")
        alice = self._login(self.alice)
        self.assertEqual(alice.get(self.url).context["my_remaining"], 2)
        self._vote(alice, self._interp_choice(self.a))
        r = alice.get(self.url)
        self.assertEqual(r.context["my_remaining"], 1)
        self.assertEqual(r.context["my_done"], 1)
        self.assertContains(r, "아직 1개 항목을 고르지 않았습니다")

    def test_host_sees_tabs_to_both_screens_once_voting_started(self):
        r = self._login(self.host).get(self.url)
        self.assertContains(r, reverse("decision_review", args=[self.meeting.id]))
        r = self._login(self.alice).get(self.url)
        self.assertNotContains(r, reverse("decision_review", args=[self.meeting.id]))

    def test_orphan_votes_are_counted_separately(self):
        # 해석이 지워진 표는 어느 보기였는지 알 수 없으므로 따로 센다(인원 합이 맞아야 하므로).
        self._vote(self._login(self.alice), self._interp_choice(self.a))
        self._vote(self._login(self.host), self._interp_choice(self.b))
        self.a.delete()
        tally = self._login(self.host).get(self.url).context["rows"][0]["tally"]
        self.assertEqual(tally["orphan"], 1)
        self.assertEqual(tally["voted"], 2)

    def test_tally_uses_a_fixed_number_of_queries(self):
        for user in (self.alice, self.bob, self.host):
            self._vote(self._login(user), self._interp_choice(self.a))
        for order in range(1, 5):
            extra = Decision.objects.create(
                meeting=self.meeting, order=order, what="항목 %d" % order,
                warnings=["담당자 빈칸"], status=Decision.STATUS_VOTING,
            )
            Interpretation.objects.create(decision=extra, order=0, text="해석 A")
            DecisionVote.objects.create(
                decision=extra, user=self.host, kind=DecisionVote.KIND_NOT_DECIDED,
            )
        client = self._login(self.host)
        with CaptureQueriesContext(connection) as many:
            client.get(self.url)
        Decision.objects.filter(order__gt=0).delete()
        with CaptureQueriesContext(connection) as few:
            client.get(self.url)
        # 항목이 1개일 때와 5개일 때 쿼리 수가 같아야 한다(집계는 한 번에 센다).
        self.assertEqual(len(many.captured_queries), len(few.captured_queries))


class DecisionJudgeTests(TestCase):
    """④ 분기 판정 규칙. 제출된 표만 보고, 미투표자는 판정에서 뺀다."""

    def setUp(self):
        self.host = User.objects.create_user("hostuser", password="pw12345!")
        self.alice = User.objects.create_user("aliceuser", password="pw12345!")
        self.bob = User.objects.create_user("bobuser", password="pw12345!")
        self.team = Team.objects.create(name="t")
        self.team.members.add(self.host, self.alice, self.bob)
        self.meeting = Meeting.objects.create(
            team=self.team, title="m", host=self.host,
            processing_status=Meeting.STATUS_DONE, transcript="전사",
        )
        self.decision = Decision.objects.create(
            meeting=self.meeting, order=0, who="민수", what="배포",
            warnings=["완료 기준 빈칸"], status=Decision.STATUS_VOTING,
        )
        self.a = Interpretation.objects.create(decision=self.decision, order=0, text="이번 주 금요일")
        self.b = Interpretation.objects.create(decision=self.decision, order=1, text="다음 주 금요일")

    def _pick(self, user, interpretation):
        DecisionVote.objects.create(
            decision=self.decision, user=user,
            kind=DecisionVote.KIND_INTERPRETATION, interpretation=interpretation,
        )

    def _not_decided(self, user):
        DecisionVote.objects.create(decision=self.decision, user=user, kind=DecisionVote.KIND_NOT_DECIDED)

    def _custom(self, user, text="내가 이해한 내용"):
        DecisionVote.objects.create(
            decision=self.decision, user=user, kind=DecisionVote.KIND_CUSTOM, custom_text=text,
        )

    def _judge(self):
        return decision_compiler.judge(self.decision, self.meeting.host_id)

    # ---- 규칙 1: 진행자가 '아직 안 정해진 것 같음' → 무조건 미결정 ----

    def test_host_choosing_not_decided_makes_it_undecided_even_if_others_agree(self):
        self._not_decided(self.host)
        self._pick(self.alice, self.a)
        self._pick(self.bob, self.a)
        self.assertEqual(self._judge(), (Decision.STATUS_UNDECIDED, ""))

    # ---- 규칙 2: 제출한 전원이 같은 해석 → 확정 자동 발행 ----

    def test_everyone_picking_the_same_interpretation_resolves_it(self):
        for user in (self.host, self.alice, self.bob):
            self._pick(user, self.b)
        self.assertEqual(self._judge(), (Decision.STATUS_RESOLVED, self.b.text))

    def test_unanimity_counts_only_submitted_votes(self):
        # 밥은 아직 안 골랐다 — 판정에서 빼고, 제출한 두 명이 같으면 확정이다.
        self._pick(self.host, self.a)
        self._pick(self.alice, self.a)
        self.assertEqual(self._judge(), (Decision.STATUS_RESOLVED, self.a.text))

    def test_one_custom_entry_breaks_unanimity_regardless_of_its_text(self):
        # 의미 비교는 하지 않는다 — 문장이 같은 뜻이어도 '직접 쓰기'가 섞이면 갈림이다.
        self._pick(self.host, self.a)
        self._pick(self.alice, self.a)
        self._custom(self.bob, self.a.text)
        self.assertEqual(self._judge(), (Decision.STATUS_PENDING_HOST, ""))

    def test_everyone_writing_their_own_is_not_unanimity(self):
        for user in (self.host, self.alice, self.bob):
            self._custom(user, "같은 문장")
        self.assertEqual(self._judge(), (Decision.STATUS_PENDING_HOST, ""))

    def test_everyone_choosing_not_decided_is_undecided_via_the_host_rule(self):
        for user in (self.host, self.alice, self.bob):
            self._not_decided(user)
        self.assertEqual(self._judge(), (Decision.STATUS_UNDECIDED, ""))

    # ---- 규칙 3: 진행자 선택이 '해석을 고른 표'의 다수와 다르면 미결정 ----

    def test_host_in_the_minority_of_interpretation_votes_makes_it_undecided(self):
        self._pick(self.host, self.a)
        self._pick(self.alice, self.b)
        self._pick(self.bob, self.b)
        self.assertEqual(self._judge(), (Decision.STATUS_UNDECIDED, ""))

    def test_participants_choosing_not_decided_do_not_push_the_host_into_a_minority(self):
        """진행자는 해석을 골랐고 참가자 일부가 '아직 안 정해진 것 같음'을 고른 경우.

        '아직 안 정해진 것 같음' 표는 해석 다수 계산에서 빠지므로 진행자가 소수가 되지 않고,
        갈림(진행자 확정 대기)으로 간다.
        """
        self._pick(self.host, self.a)
        self._not_decided(self.alice)
        self._not_decided(self.bob)
        self.assertEqual(self._judge(), (Decision.STATUS_PENDING_HOST, ""))

    def test_custom_votes_do_not_push_the_host_into_a_minority_either(self):
        self._pick(self.host, self.a)
        self._custom(self.alice)
        self._custom(self.bob)
        self.assertEqual(self._judge(), (Decision.STATUS_PENDING_HOST, ""))

    def test_a_tie_keeps_the_host_out_of_the_minority(self):
        self._pick(self.host, self.a)
        self._pick(self.alice, self.b)
        self.assertEqual(self._judge(), (Decision.STATUS_PENDING_HOST, ""))

    def test_host_writing_their_own_goes_to_pending_host(self):
        self._custom(self.host)
        self._pick(self.alice, self.b)
        self._pick(self.bob, self.b)
        self.assertEqual(self._judge(), (Decision.STATUS_PENDING_HOST, ""))

    # ---- 경계 ----

    def test_no_votes_at_all_is_undecided(self):
        self.assertEqual(self._judge(), (Decision.STATUS_UNDECIDED, ""))

    def test_host_not_voting_skips_rules_one_and_three(self):
        # 진행자 표가 없으면 참가자 표만으로 판정한다(선투표 강제가 뚫린 경우의 방어선).
        self._pick(self.alice, self.a)
        self._pick(self.bob, self.a)
        self.assertEqual(self._judge(), (Decision.STATUS_RESOLVED, self.a.text))

    def test_meeting_without_a_host_judges_on_participants_only(self):
        Meeting.objects.filter(pk=self.meeting.pk).update(host=None)
        self.meeting.refresh_from_db()
        self._pick(self.alice, self.a)
        self._pick(self.bob, self.b)
        self.assertEqual(
            decision_compiler.judge(self.decision, self.meeting.host_id),
            (Decision.STATUS_PENDING_HOST, ""),
        )

    def test_finalize_only_acts_on_a_voting_item(self):
        for user in (self.host, self.alice, self.bob):
            self._pick(user, self.a)
        self.assertEqual(decision_compiler.finalize(self.decision, self.host.id), Decision.STATUS_RESOLVED)
        self.decision.refresh_from_db()
        self.assertEqual(self.decision.final_text, self.a.text)
        # 두 번째 마감은 이미 판정된 항목을 다시 바꾸지 않는다.
        Interpretation.objects.filter(pk=self.b.pk).delete()
        self.assertEqual(decision_compiler.finalize(self.decision, self.host.id), Decision.STATUS_RESOLVED)
        self.decision.refresh_from_db()
        self.assertEqual(self.decision.final_text, self.a.text)


class DecisionBranchFlowTests(TestCase):
    """④ 분기 처리의 화면 흐름 — 자동 발행, 마감, 진행자 한 줄 확정과 동의."""

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
            meeting=self.meeting, order=0, who="민수", what="배포",
            warnings=["완료 기준 빈칸"], status=Decision.STATUS_VOTING,
        )
        self.a = Interpretation.objects.create(decision=self.decision, order=0, text="이번 주 금요일")
        self.b = Interpretation.objects.create(decision=self.decision, order=1, text="다음 주 금요일")
        self.vote_url = reverse("decision_vote", args=[self.meeting.id])
        self.review_url = reverse("decision_review", args=[self.meeting.id])

    def _login(self, user):
        client = Client()
        client.force_login(user)
        return client

    def _choice(self, interpretation):
        return "%s%d" % (decision_compiler.VOTE_CHOICE_PREFIX, interpretation.id)

    def _vote(self, user, choice, custom_text=""):
        return self._login(user).post(self.vote_url, {
            "decision_id": self.decision.id, "choice": choice, "custom_text": custom_text,
        })

    # ---- 전원이 골랐을 때 자동 판정 ----

    def test_item_is_published_as_soon_as_everyone_picks_the_same_interpretation(self):
        self._vote(self.alice, self._choice(self.a))
        self._vote(self.bob, self._choice(self.a))
        self.decision.refresh_from_db()
        self.assertEqual(self.decision.status, Decision.STATUS_VOTING)
        self._vote(self.host, self._choice(self.a))
        self.decision.refresh_from_db()
        self.assertEqual(self.decision.status, Decision.STATUS_RESOLVED)
        self.assertEqual(self.decision.final_text, self.a.text)

    def test_everyone_voting_with_a_split_goes_to_pending_host(self):
        self._vote(self.alice, self._choice(self.a))
        self._vote(self.bob, DecisionVote.KIND_CUSTOM, "내 해석")
        self._vote(self.host, self._choice(self.a))
        self.decision.refresh_from_db()
        self.assertEqual(self.decision.status, Decision.STATUS_PENDING_HOST)
        self.assertEqual(self.decision.final_text, "")

    def test_host_choosing_not_decided_marks_it_undecided_on_the_spot(self):
        self._vote(self.alice, self._choice(self.a))
        self._vote(self.bob, self._choice(self.a))
        self._vote(self.host, DecisionVote.KIND_NOT_DECIDED)
        self.decision.refresh_from_db()
        self.assertEqual(self.decision.status, Decision.STATUS_UNDECIDED)

    def test_resolved_item_stops_accepting_votes(self):
        for user in (self.alice, self.bob, self.host):
            self._vote(user, self._choice(self.a))
        extra = User.objects.create_user("lateuser", password="pw12345!")
        self.team.members.add(extra)
        r = self._vote(extra, self._choice(self.b))
        self.assertContains(r, "받고 있는 항목이 아닙니다")

    # ---- 진행자 마감과 선투표 강제 ----

    def test_host_cannot_close_before_voting_themselves(self):
        self._vote(self.alice, self._choice(self.a))
        r = self._login(self.host).post(self.review_url, {"action": "close_vote"})
        self.assertContains(r, "본인 선택을 제출해야 마감할 수 있습니다")
        self.decision.refresh_from_db()
        self.assertEqual(self.decision.status, Decision.STATUS_VOTING)

    def test_close_button_is_disabled_until_the_host_votes(self):
        r = self._login(self.host).get(self.review_url)
        self.assertFalse(r.context["can_close"])
        self.assertEqual(r.context["host_remaining"], 1)
        self.assertContains(r, "disabled")
        self._vote(self.host, self._choice(self.a))
        r = self._login(self.host).get(self.review_url)
        self.assertTrue(r.context["can_close"])
        self.assertEqual(r.context["host_remaining"], 0)

    def test_closing_judges_with_submitted_votes_only(self):
        self._vote(self.host, self._choice(self.a))
        self._vote(self.alice, self._choice(self.a))
        # 밥은 안 골랐다 — 판정에서 빠지므로 제출한 두 명이 같아서 확정된다.
        self._login(self.host).post(self.review_url, {"action": "close_vote"})
        self.decision.refresh_from_db()
        self.assertEqual(self.decision.status, Decision.STATUS_RESOLVED)

    def test_missing_voters_are_reported_as_a_count_only(self):
        self._vote(self.host, self._choice(self.a))
        self._vote(self.alice, DecisionVote.KIND_CUSTOM, "내 해석")
        self._login(self.host).post(self.review_url, {"action": "close_vote"})
        r = self._login(self.host).get(self.vote_url)
        tally = r.context["rows"][0]["tally"]
        self.assertEqual(tally["missing"], 1)
        self.assertContains(r, "1명 미선택")
        self.assertNotContains(r, self.bob.username)

    def test_closing_with_nothing_open_is_refused(self):
        self._vote(self.host, self._choice(self.a))
        self._login(self.host).post(self.review_url, {"action": "close_vote"})
        r = self._login(self.host).post(self.review_url, {"action": "close_vote"})
        self.assertContains(r, "마감할 항목이 없습니다")

    def test_non_host_cannot_close(self):
        self.assertEqual(
            self._login(self.alice).post(self.review_url, {"action": "close_vote"}).status_code, 403,
        )

    # ---- 진행자 한 줄 확정과 동의 ----

    def _to_pending_host(self):
        self._vote(self.host, self._choice(self.a))
        self._vote(self.alice, self._choice(self.b))
        self._vote(self.bob, DecisionVote.KIND_CUSTOM, "내 해석")
        self.decision.refresh_from_db()
        self.assertEqual(self.decision.status, Decision.STATUS_PENDING_HOST)

    def test_host_writes_one_line_and_members_agree_to_resolve_it(self):
        self._to_pending_host()
        self._login(self.host).post(self.review_url, {
            "action": "confirm", "decision_id": self.decision.id,
            "final_text": "금요일 18시까지 스테이징 배포",
        })
        self.decision.refresh_from_db()
        self.assertEqual(self.decision.status, Decision.STATUS_PENDING_HOST)
        self.assertEqual(self.decision.final_text, "금요일 18시까지 스테이징 배포")

        self._login(self.alice).post(self.vote_url, {"action": "agree", "decision_id": self.decision.id})
        self.decision.refresh_from_db()
        self.assertEqual(self.decision.status, Decision.STATUS_PENDING_HOST)
        self._login(self.bob).post(self.vote_url, {"action": "agree", "decision_id": self.decision.id})
        self.decision.refresh_from_db()
        self.assertEqual(self.decision.status, Decision.STATUS_RESOLVED)
        self.assertEqual(self.decision.final_text, "금요일 18시까지 스테이징 배포")

    def test_members_see_the_confirmed_sentence_and_the_agree_button(self):
        self._to_pending_host()
        self._login(self.host).post(self.review_url, {
            "action": "confirm", "decision_id": self.decision.id, "final_text": "금요일 18시까지",
        })
        r = self._login(self.alice).get(self.vote_url)
        self.assertContains(r, "금요일 18시까지")
        self.assertContains(r, "이 내용으로 맞춤")
        self.assertTrue(r.context["rows"][0]["needs_my_agreement"])

    def test_the_host_is_not_an_agreement_target(self):
        self._to_pending_host()
        self._login(self.host).post(self.review_url, {
            "action": "confirm", "decision_id": self.decision.id, "final_text": "금요일 18시까지",
        })
        r = self._login(self.host).post(self.vote_url, {"action": "agree", "decision_id": self.decision.id})
        self.assertContains(r, "진행자는 따로 동의하지 않습니다")
        self.assertEqual(self.decision.agreements.count(), 0)
        # 동의가 필요한 인원은 진행자를 뺀 2명이다.
        self.assertEqual(decision_compiler.agreement_total(self.meeting), 2)

    def test_agreeing_twice_counts_once(self):
        self._to_pending_host()
        self._login(self.host).post(self.review_url, {
            "action": "confirm", "decision_id": self.decision.id, "final_text": "금요일 18시까지",
        })
        alice = self._login(self.alice)
        alice.post(self.vote_url, {"action": "agree", "decision_id": self.decision.id})
        alice.post(self.vote_url, {"action": "agree", "decision_id": self.decision.id})
        self.assertEqual(self.decision.agreements.count(), 1)

    def test_rewriting_the_sentence_invalidates_earlier_agreements(self):
        self._to_pending_host()
        host = self._login(self.host)
        host.post(self.review_url, {
            "action": "confirm", "decision_id": self.decision.id, "final_text": "금요일 18시까지",
        })
        self._login(self.alice).post(self.vote_url, {"action": "agree", "decision_id": self.decision.id})
        self.assertEqual(self.decision.agreements.count(), 1)
        host.post(self.review_url, {
            "action": "confirm", "decision_id": self.decision.id, "final_text": "월요일 10시까지",
        })
        self.assertEqual(self.decision.agreements.count(), 0)
        self.decision.refresh_from_db()
        self.assertEqual(self.decision.status, Decision.STATUS_PENDING_HOST)

    def test_confirm_requires_a_sentence(self):
        self._to_pending_host()
        r = self._login(self.host).post(self.review_url, {
            "action": "confirm", "decision_id": self.decision.id, "final_text": "   ",
        })
        self.assertContains(r, "한 줄 적어 주세요")
        self.decision.refresh_from_db()
        self.assertEqual(self.decision.final_text, "")

    def test_confirm_is_refused_on_an_item_that_is_not_pending_host(self):
        r = self._login(self.host).post(self.review_url, {
            "action": "confirm", "decision_id": self.decision.id, "final_text": "아무 문장",
        })
        self.assertContains(r, "진행자 확정을 기다리는 항목이 아닙니다")

    def test_agreeing_before_the_host_writes_anything_is_refused(self):
        self._to_pending_host()
        r = self._login(self.alice).post(self.vote_url, {"action": "agree", "decision_id": self.decision.id})
        self.assertContains(r, "동의를 받고 있는 항목이 아닙니다")
        self.assertEqual(self.decision.agreements.count(), 0)

    def test_non_host_cannot_confirm(self):
        self._to_pending_host()
        self.assertEqual(
            self._login(self.alice).post(self.review_url, {
                "action": "confirm", "decision_id": self.decision.id, "final_text": "내가 확정",
            }).status_code, 403,
        )

    def test_solo_team_resolves_as_soon_as_the_host_writes_the_sentence(self):
        solo_team = Team.objects.create(name="solo")
        solo_team.members.add(self.host)
        solo = Meeting.objects.create(
            team=solo_team, title="solo", host=self.host,
            processing_status=Meeting.STATUS_DONE, transcript="전사",
        )
        decision = Decision.objects.create(
            meeting=solo, order=0, what="혼자 결정", warnings=["담당자 빈칸"],
            status=Decision.STATUS_VOTING,
        )
        Interpretation.objects.create(decision=decision, order=0, text="해석 A")
        Interpretation.objects.create(decision=decision, order=1, text="해석 B")
        host = self._login(self.host)
        host.post(reverse("decision_vote", args=[solo.id]), {
            "decision_id": decision.id, "choice": DecisionVote.KIND_CUSTOM, "custom_text": "내 해석",
        })
        decision.refresh_from_db()
        self.assertEqual(decision.status, Decision.STATUS_PENDING_HOST)
        host.post(reverse("decision_review", args=[solo.id]), {
            "action": "confirm", "decision_id": decision.id, "final_text": "혼자 확정",
        })
        decision.refresh_from_db()
        self.assertEqual(decision.status, Decision.STATUS_RESOLVED)


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

    def test_resolved_and_pending_items_are_not_carried_over(self):
        for status in (Decision.STATUS_RESOLVED, Decision.STATUS_PENDING_HOST, Decision.STATUS_VOTING, Decision.STATUS_DRAFT):
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
        r = self._login(self.host).get(self.detail_url)
        self.assertNotContains(r, self.member.username)


class DecisionModelTests(TestCase):
    """결정 컴파일러(실험 기능) 모델 테스트. 기존 CheckQuestion/Answer 테이블과 완전히
    분리된 새 테이블이라는 것과, 중복 투표를 DB가 막아준다는 것을 고정한다."""

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

    def test_decision_defaults_to_draft_with_no_carryover(self):
        decision = self._decision()
        self.assertEqual(decision.status, Decision.STATUS_DRAFT)
        # 미결정으로 이월된 안건만 carryover_resolved_at을 쓰므로 기본값은 비어 있어야 한다.
        self.assertIsNone(decision.carryover_resolved_at)
        self.assertTrue(decision.has_warning)

    def test_warnings_json_round_trips_as_a_list(self):
        decision = self._decision()
        reloaded = Decision.objects.get(pk=decision.pk)
        self.assertEqual(reloaded.warnings, ["모호어 '쯤'", "완료 기준 빈칸"])
        empty = self._decision(order=1, warnings=[])
        self.assertEqual(Decision.objects.get(pk=empty.pk).warnings, [])
        self.assertFalse(empty.has_warning)

    def test_spec_line_shows_blank_fields_as_question_marks(self):
        decision = self._decision(done_criteria="", confirm_state=Decision.CONFIRM_UNSET)
        self.assertEqual(decision.spec_line, "민수 · 배포 준비 · 다음 주쯤 · ? · 미정")

    def test_duplicate_decision_order_in_one_meeting_is_rejected_by_db(self):
        self._decision(order=0)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._decision(order=0, who="다른 사람")

    def test_same_order_is_allowed_in_a_different_meeting(self):
        other = Meeting.objects.create(team=self.team, title="m2", host=self.host)
        self._decision(order=0)
        Decision.objects.create(meeting=other, order=0, who="민수", what="배포 준비")
        self.assertEqual(Decision.objects.filter(order=0).count(), 2)

    def test_interpretation_labels_follow_order(self):
        decision = self._decision()
        labels = [
            Interpretation.objects.create(decision=decision, order=i, text="해석 %d" % i, evidence="민수가 다음 주쯤").label
            for i in range(3)
        ]
        self.assertEqual(labels, ["A", "B", "C"])

    def test_duplicate_interpretation_order_is_rejected_by_db(self):
        decision = self._decision()
        Interpretation.objects.create(decision=decision, order=0, text="해석 A")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Interpretation.objects.create(decision=decision, order=0, text="해석 B")

    def test_one_vote_per_user_per_decision(self):
        decision = self._decision()
        interpretation = Interpretation.objects.create(decision=decision, order=0, text="해석 A")
        DecisionVote.objects.create(
            decision=decision, user=self.member,
            kind=DecisionVote.KIND_INTERPRETATION, interpretation=interpretation,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            DecisionVote.objects.create(
                decision=decision, user=self.member, kind=DecisionVote.KIND_NOT_DECIDED,
            )

    def test_different_users_vote_on_the_same_decision(self):
        decision = self._decision()
        DecisionVote.objects.create(decision=decision, user=self.host, kind=DecisionVote.KIND_NOT_DECIDED)
        DecisionVote.objects.create(
            decision=decision, user=self.member, kind=DecisionVote.KIND_CUSTOM, custom_text="금요일까지 스테이징 배포",
        )
        self.assertEqual(decision.votes.count(), 2)

    def test_deleting_an_interpretation_keeps_the_vote_row(self):
        # 진행자가 해석 후보를 지워도 '누가 투표했는지'는 남아야 중복 투표 방지가 유지된다.
        decision = self._decision()
        interpretation = Interpretation.objects.create(decision=decision, order=0, text="해석 A")
        DecisionVote.objects.create(
            decision=decision, user=self.member,
            kind=DecisionVote.KIND_INTERPRETATION, interpretation=interpretation,
        )
        interpretation.delete()
        vote = DecisionVote.objects.get(decision=decision, user=self.member)
        self.assertIsNone(vote.interpretation_id)

    def test_one_agreement_per_user_per_decision(self):
        decision = self._decision(status=Decision.STATUS_PENDING_HOST, final_text="금요일까지 스테이징 배포")
        DecisionAgreement.objects.create(decision=decision, user=self.member)
        with self.assertRaises(IntegrityError), transaction.atomic():
            DecisionAgreement.objects.create(decision=decision, user=self.member)

    def test_one_checkpoint_per_user_per_decision(self):
        decision = self._decision(status=Decision.STATUS_RESOLVED, final_text="금요일까지 스테이징 배포")
        checkpoint = Checkpoint.objects.create(decision=decision, user=self.member, expected_text="배포 문서 작성")
        self.assertEqual(checkpoint.verdict, Checkpoint.VERDICT_UNKNOWN)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Checkpoint.objects.create(decision=decision, user=self.member, expected_text="다른 내용")

    def test_deleting_the_meeting_removes_the_whole_decision_tree(self):
        decision = self._decision()
        interpretation = Interpretation.objects.create(decision=decision, order=0, text="해석 A")
        DecisionVote.objects.create(
            decision=decision, user=self.member,
            kind=DecisionVote.KIND_INTERPRETATION, interpretation=interpretation,
        )
        DecisionAgreement.objects.create(decision=decision, user=self.member)
        Checkpoint.objects.create(decision=decision, user=self.member, expected_text="배포 문서 작성")
        self.meeting.delete()
        self.assertEqual(Decision.objects.count(), 0)
        self.assertEqual(Interpretation.objects.count(), 0)
        self.assertEqual(DecisionVote.objects.count(), 0)
        self.assertEqual(DecisionAgreement.objects.count(), 0)
        self.assertEqual(Checkpoint.objects.count(), 0)

    def test_existing_quiz_tables_are_untouched_by_the_new_feature(self):
        # 새 기능은 기존 퀴즈 흐름 옆에 추가만 한다 — 같은 회의에 둘이 공존해야 한다.
        question = CheckQuestion.objects.create(
            meeting=self.meeting, text="q", choices=["1", "2", "3", "4"],
            correct_choice=0, status=CheckQuestion.STATUS_PUBLISHED, order=0,
        )
        Answer.objects.create(question=question, user=self.member, selected_choice=0)
        self._decision()
        self.assertEqual(self.meeting.check_questions.count(), 1)
        self.assertEqual(self.meeting.decisions.count(), 1)
        self.assertEqual(Answer.objects.filter(question=question).count(), 1)
