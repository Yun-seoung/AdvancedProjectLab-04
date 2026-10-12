from datetime import timedelta

from django.utils import timezone

from .models import Meeting


def due_soon(request):
    """내일 마감인 회의를 모든 화면 상단 배너에서 보여주기 위한 전역 컨텍스트."""
    if not request.user.is_authenticated:
        return {}
    meetings = Meeting.objects.filter(
        team__members=request.user, deadline=timezone.localdate() + timedelta(days=1)
    ).select_related("team")
    return {"due_soon": meetings}
