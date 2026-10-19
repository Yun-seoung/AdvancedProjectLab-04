import json
import os
import sys
import tempfile
from unittest.mock import MagicMock, patch
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import services, tasks, views
from .models import Answer, ChatMessage, CheckQuestion, Meeting, Question, Recording, Team


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

        with patch.dict(os.environ, {"OLLAMA_MODEL": "m1"}), patch("urllib.request.urlopen", side_effect=fake_urlopen):
            services._generate("hi")
        self.assertEqual(captured["body"]["options"]["num_ctx"], 16384)


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
