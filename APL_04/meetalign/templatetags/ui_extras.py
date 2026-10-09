"""화면 표현만 돕는 템플릿 필터. 모델·뷰·context 는 건드리지 않는다."""

from django import template

register = template.Library()


@register.filter
def md_sections(text):
    """저장된 통합 회의록 마크다운을 섹션 카드용 목록으로 쪼갠다.

    '## 제목' 을 섹션 경계로 보고, '- ' 로 시작하는 줄은 항목으로 모은다.
    반환: [{"heading": str, "items": [str], "paragraphs": [str]}, ...]
    """
    sections = []
    current = None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("# "):
            continue
        if line.startswith("## "):
            current = {"heading": line[3:].strip(), "items": [], "paragraphs": []}
            sections.append(current)
            continue
        if current is None:
            continue
        if line.startswith("- "):
            current["items"].append(line[2:].strip())
        else:
            current["paragraphs"].append(line)
    return sections


@register.filter
def top_wrong_index(choices):
    """집계에서 '가장 많이 선택된 오답'의 보기 번호를 돌려준다. 없으면 0.

    choices 는 뷰가 넘기는 [{"index", "count", "is_answer", ...}] 목록이다.
    """
    best = None
    for choice in choices or []:
        if choice.get("is_answer") or not choice.get("count"):
            continue
        if best is None or choice["count"] > best["count"]:
            best = choice
    return best["index"] if best else 0


@register.filter
def percent(count, total):
    """집계 가로 막대의 길이(%)를 계산한다."""
    try:
        count = int(count)
        total = int(total)
    except (TypeError, ValueError):
        return 0
    if total <= 0:
        return 0
    return round(count * 100 / total)
