from django.test import TestCase

from .models import AnonQuestion


class AnonQuestionTests(TestCase):
    def test_model_has_no_author_field(self):
        """익명 질문 모델에 작성자를 가리키는 필드가 없어야 한다."""
        field_names = {f.name for f in AnonQuestion._meta.get_fields()}
        for forbidden in ("user", "author", "created_by", "writer", "owner"):
            self.assertNotIn(forbidden, field_names)

        # 사용자 모델을 가리키는 관계 필드가 아예 없는지도 확인한다.
        related = [
            f.name for f in AnonQuestion._meta.get_fields()
            if getattr(f, "related_model", None) is not None
            and f.related_model._meta.label == "auth.User"
        ]
        self.assertEqual(related, [])
