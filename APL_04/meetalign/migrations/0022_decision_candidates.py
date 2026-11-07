"""확정 문장 후보 재설계 — 칸 2개 추가와 공개 문장 보정.

- Decision.divergence_reason: AI가 쓴 "왜 다르게 이해할 수 있나" 한 줄.
- Interpretation.recommended: 근거가 가장 강한 후보 하나에 붙는 'AI 추천' 표시.
- 데이터 보정: published_text의 폴백(final_text or spec_line)을 지우면, 이미 공개된
  레코드 중 final_text가 빈 것들은 팀원 화면에서 문장이 사라진다. 그 레코드만 기존 네 칸
  (누가·무엇을·언제까지·완료 기준)으로 만든 결정 사항 한 줄로 채운다.
  draft 레코드는 다시 추출하면 되므로 건드리지 않고, 이미 문장이 있는 레코드는 덮지 않는다.
"""

from django.db import migrations, models


def spec_sentence(who, what, due, done):
    """기존 네 칸으로 결정 사항 한 줄을 만든다. 빈칸은 건너뛴다.

    폴백이 쓰던 spec_line은 빈칸을 '?'로 드러냈지만, 여기서는 사람이 읽을 공개 문장을
    만드는 것이므로 물음표를 남기지 않는다.
    """
    return " · ".join(part for part in (who, what, due, done) if part)


# 미결정 항목의 꼬리말. decision_compiler.undecided_sentence()와 같은 문구이며 쌍으로 고쳐야
# 한다 — 마이그레이션은 앱 코드를 import하면 안 되므로(나중에 그 함수가 바뀌어도 과거
# 마이그레이션의 결과는 그대로여야 한다) 문구를 여기 복사해 둔다.
CARRIED_OVER_SUFFIX = " — 이번 회의에서 결론이 나지 않았음 (다음 회의로)"


def fill_missing_final_text(Decision):
    """공개(published/undecided)됐는데 확정 문장이 빈 레코드만 채우고, 채운 수를 돌려준다.

    미결정 행에는 꼬리말을 붙인다. 팀원 화면이 상태 분기 없이 final_text만 보여주므로,
    꼬리말이 없으면 다음 회의로 넘어간 안건이 보통 확정 결정과 똑같이 읽힌다.
    """
    filled = 0
    rows = Decision.objects.filter(status__in=("published", "undecided"), final_text="")
    for decision in rows:
        sentence = spec_sentence(
            decision.who, decision.what, decision.due_text, decision.done_criteria,
        ) or decision.what
        if not sentence:
            continue
        if decision.status == "undecided":
            sentence += CARRIED_OVER_SUFFIX
        Decision.objects.filter(pk=decision.pk).update(final_text=sentence)
        filled += 1
    return filled


def _forward(apps, schema_editor):
    fill_missing_final_text(apps.get_model("meetalign", "Decision"))


class Migration(migrations.Migration):

    dependencies = [
        ("meetalign", "0021_alter_answer_unique_together_remove_answer_question_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="decision",
            name="divergence_reason",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="interpretation",
            name="recommended",
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(_forward, migrations.RunPython.noop),
    ]
