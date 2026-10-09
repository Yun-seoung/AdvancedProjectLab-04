"""LLM 격리 계층.

공급자는 환경변수 LLM_PROVIDER 로 고른다 (mock | gemini).
뷰는 make_minutes() / make_quiz() 만 호출한다.
"""

import os
import re

QUIZ_SIZE = 5


class LlmError(Exception):
    """회의록/문제 생성에 실패했을 때. 뷰는 이 메시지를 화면에 보여준다."""


def provider_name():
    return (os.environ.get("LLM_PROVIDER") or "mock").strip().lower()


# --------------------------------------------------------------------------
# 공개 API
# --------------------------------------------------------------------------

def make_minutes(transcript, title=""):
    """전사 텍스트에서 통합 회의록(마크다운)을 만든다."""
    provider = provider_name()
    if provider == "mock":
        return _mock_minutes(transcript, title)
    raise LlmError("지원하지 않는 LLM_PROVIDER 입니다: %s" % provider)


def make_quiz(transcript, minutes=""):
    """이해가 엇갈릴 만한 지점을 묻는 확인 문제 5개를 만든다.

    각 문제는 {kind, text, choices(4개), answer_index(1~4), evidence} 꼴이다.
    """
    provider = provider_name()
    if provider == "mock":
        return _mock_quiz(transcript, minutes)
    raise LlmError("지원하지 않는 LLM_PROVIDER 입니다: %s" % provider)


# --------------------------------------------------------------------------
# 공용 도우미
# --------------------------------------------------------------------------

def split_sentences(text):
    """전사/회의록을 문장 단위로 쪼갠다."""
    parts = re.split(r"(?<=[.!?])\s+|\n+", text or "")
    return [p.strip() for p in parts if p.strip()]


# --------------------------------------------------------------------------
# mock 공급자
# --------------------------------------------------------------------------

_SECTIONS = [
    ("결정된 사항", ("결정", "확정", "포함하기로", "제한합니다", "잡았습니다", "두기로")),
    ("아직 논의 중인 사항", ("논의 중", "미결", "다음 회의", "검토", "쟁점")),
    ("담당", ("맡", "담당")),
    ("숫자와 기한", ("월 ", "일입니다", "명", "원", "%")),
    ("용어", ("정의", "뜻", "라고 부")),
]


def _mock_minutes(transcript, title):
    sentences = split_sentences(transcript)
    if not sentences:
        raise LlmError("전사 텍스트가 비어 있어 회의록을 만들 수 없습니다.")

    lines = ["# %s 통합 회의록" % (title or "회의"), ""]
    lines.append("> mock 공급자로 만든 회의록입니다. 실제 LLM 연결은 Phase 2에서 합니다.")
    lines.append("")
    for heading, keywords in _SECTIONS:
        picked = [s for s in sentences if any(k in s for k in keywords)]
        lines.append("## %s" % heading)
        if picked:
            lines.extend("- %s" % s for s in picked)
        else:
            lines.append("- (해당 내용을 찾지 못했습니다)")
        lines.append("")
    return "\n".join(lines).strip()


#: 유형별 문항 틀. (kind, 문항, 근거 문장을 찾는 정규식들(앞쪽 우선), 오답 보기 3개)
_QUIZ_PLAN = [
    (
        "number",
        "회의에서 합의된 숫자(금액·인원 등)에 대한 설명으로 맞는 것은?",
        [re.compile(r"\d+\s*(만|천|명|원|개|%|퍼센트)")],
        ["숫자는 정해지지 않았다", "숫자는 다음 회의에서 정하기로 했다", "담당자가 각자 알아서 정한다"],
    ),
    (
        "deadline",
        "회의에서 정한 일정·기한에 대한 설명으로 맞는 것은?",
        [re.compile(r"\d+\s*월\s*\d+\s*일|프리즈|마감|기한"), re.compile(r"\d+\s*월|\d+\s*일|일정")],
        ["기한은 정해지지 않았다", "기한은 다음 달로 미뤘다", "기한은 담당자가 각자 정한다"],
    ),
    (
        "owner",
        "담당자 배정에 대한 설명으로 맞는 것은?",
        [re.compile(r"맡|담당")],
        ["담당자는 정해지지 않았다", "모든 작업을 진행자 한 사람이 맡는다", "담당은 다음 회의에서 정한다"],
    ),
    (
        "term",
        "회의에서 쓴 용어의 정의로 맞는 것은?",
        [re.compile(r"정의|뜻|라고 부")],
        ["용어의 정의는 합의되지 않았다", "용어는 사람마다 다르게 써도 된다고 했다", "용어 정의는 다음 회의로 넘겼다"],
    ),
    (
        "scope",
        "이번 작업 범위에 대한 설명으로 맞는 것은?",
        [re.compile(r"제외|포함|빼"), re.compile(r"범위")],
        ["범위는 아직 정해지지 않았다", "모든 기능을 이번 범위에 넣는다", "범위는 담당자가 각자 판단한다"],
    ),
    (
        "status",
        "확정된 것과 논의 중인 것에 대한 설명으로 맞는 것은?",
        [re.compile(r"논의 중|미결"), re.compile(r"확정|결정")],
        ["회의에서 다루지 않았다", "이미 확정되어 바꿀 수 없다", "진행자 한 사람이 혼자 결정했다"],
    ),
]


def _shorten(sentence, limit=44):
    return sentence if len(sentence) <= limit else sentence[: limit - 1] + "…"


def _mock_quiz(transcript, minutes):
    sentences = split_sentences(transcript) or split_sentences(minutes)
    if not sentences:
        raise LlmError("전사 텍스트가 비어 있어 확인 문제를 만들 수 없습니다.")

    items = []
    used = set()
    # 1차: 유형별로 맞는 문장을 찾아 문제를 만든다.
    for kind, text, patterns, distractors in _QUIZ_PLAN:
        if len(items) >= QUIZ_SIZE:
            break
        evidence = _find_evidence(sentences, used, patterns)
        if evidence is None:
            continue
        used.add(evidence)
        items.append(_build_item(kind, text, evidence, distractors, len(items)))

    # 2차: 유형이 모자라면 남은 문장으로 채워 항상 5문제를 만든다.
    leftovers = [s for s in sentences if s not in used]
    while len(items) < QUIZ_SIZE and leftovers:
        evidence = leftovers.pop(0)
        used.add(evidence)
        items.append(_build_item(
            "status",
            "다음 내용에 대해 회의에서 합의된 것은?",
            evidence,
            ["회의에서 다루지 않았다", "다음 회의로 미뤘다", "참가자마다 다르게 이해해도 된다고 했다"],
            len(items),
        ))

    if len(items) < QUIZ_SIZE:
        raise LlmError("확인 문제 %d개를 만들기에 전사 텍스트가 너무 짧습니다." % QUIZ_SIZE)
    return items


def _find_evidence(sentences, used, patterns):
    """앞쪽 정규식에 맞는 문장을 먼저 고른다 (더 구체적인 근거를 우선)."""
    for pattern in patterns:
        for sentence in sentences:
            if sentence not in used and pattern.search(sentence):
                return sentence
    return None


def _build_item(kind, text, evidence, distractors, index):
    """정답 위치를 1→2→3→4로 돌려가며 보기를 배치한다.

    보기는 읽기 좋게 짧게 줄이고, 근거에는 원문 문장을 그대로 남긴다.
    """
    answer_index = index % 4 + 1
    choices = list(distractors)
    choices.insert(answer_index - 1, _shorten(evidence))
    return {
        "kind": kind,
        "text": text,
        "choices": choices[:4],
        "answer_index": answer_index,
        "evidence": evidence,
    }
