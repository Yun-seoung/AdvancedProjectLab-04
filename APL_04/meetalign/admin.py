from django.contrib import admin

from .models import Checkpoint, Decision, DecisionAgreement, Interpretation

# 결정 컴파일러(실험 기능)만 관리자에 등록한다. 기존 모델은 지금까지와 같이 등록하지 않는다.


class InterpretationInline(admin.TabularInline):
    model = Interpretation
    extra = 0


@admin.register(Decision)
class DecisionAdmin(admin.ModelAdmin):
    list_display = (
        "id", "meeting", "order", "who", "confirm_state", "status",
        "marked_undecided", "revised", "carryover_resolved_at",
    )
    list_filter = ("status", "confirm_state", "marked_undecided", "revised")
    search_fields = ("who", "what", "final_text")
    inlines = [InterpretationInline]


@admin.register(Interpretation)
class InterpretationAdmin(admin.ModelAdmin):
    list_display = ("id", "decision", "order", "text")


@admin.register(DecisionAgreement)
class DecisionAgreementAdmin(admin.ModelAdmin):
    # 운영자 디버깅용. 서비스 화면에는 누가 differ를 골랐는지 절대 내보내지 않는다.
    list_display = ("id", "decision", "user", "kind", "updated_at")
    list_filter = ("kind",)


@admin.register(Checkpoint)
class CheckpointAdmin(admin.ModelAdmin):
    list_display = ("id", "decision", "user", "verdict")
    list_filter = ("verdict",)
