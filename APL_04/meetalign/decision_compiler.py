"""결정 컴파일러(실험 기능) — 확정 문장 후보를 만들고 진행자가 고르게 한다.

AI를 한 번 부른다. 전사 원문을 보내고 결정마다
  [안건 한 줄 · 모호 여부 · 왜 갈리나 · 확정 문장 후보 2~3개 · 원문 근거 · 추천]
을 한 응답으로 받는다(parse_items).

- 모호한 결정: AI가 "왜 다르게 이해할 수 있나" 한 줄과 그대로 공개될 확정 문장 후보 2~3개를
  만든다. 진행자는 읽고 탭 한 번으로 고른다. 근거가 전사 원문에 실제로 없는 후보는 버린다.
  근거가 가장 강한 후보 하나에 'AI 추천' 배지가 붙지만 미리 선택해 두지는 않는다 —
  진행자가 읽고 직접 골라야 모호함이 실제로 정리되기 때문이다.
- 명확한 결정: AI가 확정 문장 하나를 쓰고, 진행자 탭 없이 공개 목록에 들어간다.
- 규칙 안전망(safety_warnings): AI가 '명확'이라 한 항목만 되짚는다. 확정 문장이나 원문 근거에
  모호어·날짜·숫자 충돌이 있으면 확인 필요로 올리고, AI가 쓴 문장을 후보 하나로 보여준다.
  빈칸만으로는 묻지 않는다 — 빈칸 경고가 거의 모든 결정에 붙어 표시가 의미를 잃었기 때문이다.

공개되는 것은 어떤 상태에서나 Decision.final_text 문장 하나다. '다음 회의로 넘김'을 고른
항목도 undecided_sentence()로 문장을 받는다.

※ 블라인드 투표 모드(보류): 팀원 전원이 후보를 블라인드로 동시 선택하고 집계로 판정하는
   방식을 먼저 만들었다가(DecisionVote + tallies_for/judge/finalize) 진행자 확정 모드로
   방향을 바꿨다. 되살릴 때 참고할 구현은 커밋 7eb074a(블라인드 선택 화면)과
   ac6fb89(전원일치/갈림/미결정 판정)에 있다.

※ services.py의 내부(_로 시작하는) 함수에 의존한다. 아래 셋의 이름이나 동작이 바뀌면 이
   모듈도 같이 고쳐야 한다.
   - services._enabled()
       Gemini(GEMINI_API_KEY+GEMINI_MODEL) · Groq(GROQ_API_KEY+GROQ_LLM_MODEL) · Ollama(OLLAMA_MODEL)
       중 하나라도 설정돼 있는지. False면 LLM을 아예 부르지 않고 가짜 응답(FAKE_DECISIONS)으로
       전체 흐름을 시연한다.
   - services._generate_clean(prompt, retries=1, temperature=None)
       프롬프트를 Gemini → Groq → Ollama 순으로 보내고 응답 문자열을 돌려준다. 한국어/영어 외
       언어가 섞이면 재시도 후 None을 주고, 연결 자체가 실패하면 OSError를 올린다.
   - services._normalize_for_match(text)
       공백을 모두 지운 비교용 문자열. 근거 문장이 전사 원문에 있는지 볼 때 쓴다.
"""

import json
import re

from django.db import IntegrityError, transaction

from . import services
from .models import Decision, DecisionAgreement, Interpretation

# 한 회의에서 받을 결정 수 상한(LLM이 회의록을 잘게 쪼개는 경우 방지).
MAX_DECISIONS = 12
# 결정 하나에 보여줄 확정 문장 후보 수 상한.
MAX_INTERPRETATIONS = 3
# 파싱 실패 시 같은 프롬프트로 한 번 더 보내고, 그래도 안 되면 실패 처리한다.
PARSE_RETRIES = 1
# 확인이 필요한 결정 수에는 상한을 두지 않는다. 모호 판정이 AI로 넘어간 뒤에 서버가 앞쪽
# 몇 개만 남기면, 모호하다고 판정된 항목이 진행자 동의 없이 조용히 공개되거나 자동으로
# 이월된다. 상한은 프롬프트로만 유도한다(_compile_prompt).

# 확정 문장에 섞이면 사람마다 다르게 읽는 표현. 팀에서 합의한 목록이라 자동 추출이 아니라 고정 목록이다.
AMBIGUOUS_PHRASES = ("쯤", "시간 되면", "각자", "나중에", "아까 정한 대로", "적당히", "대충")

# 지시어·대명사. 전사본을 훑어 "이 문장은 결정으로 다룰지 반드시 검토하라"고 LLM에 미리 짚어주는
# 용도로만 쓴다(아래 _review_hints) — AMBIGUOUS_PHRASES와 달리 이것만으로 바로 확인 필요를
# 만들지는 않는다. 일상 대화에 워낙 흔히 섞여서, safety_warnings처럼 바로 확인 필요로 만드는
# 용도로 쓰면 '명확' 판정을 과도하게 뒤집어 버린다.
REFERENT_PHRASES = ("그쪽", "그분", "그 사람", "그거", "저쪽")

# 결론을 다음으로 미루는 표현. 후보 근거 문장에 있으면 AI가 뭐라고 판단했든 '다음 회의로 넘김'을
# compile 시점에 기본값으로 걸어 둔다(_looks_deferred) — 생성형 모델은 "미정"보다 그럴듯한 답을
# 내놓는 쪽으로 치우치기 쉽고, 프롬프트만으로는 그 습성이 쉽게 재발하기 때문이다. 진행자는 화면에서
# 언제든 다른 후보나 직접 작성으로 바꿀 수 있다.
DEFERRAL_PHRASES = ("내일 다시", "다음에 얘기", "나중에 정하", "일단 둘 다", "보류")


# 날짜 표현 하나를 통째로 잡는다. "12월 5일"을 "12월"과 "5일" 두 개로 쪼개면 날짜가 하나뿐인
# 문장에도 충돌 사유가 붙으므로, 긴 형태를 먼저 매칭하도록 순서를 고정했다.
# "30일로"(기간)와 "5일까지"(기한)를 가려내려고 조사를 본다. 둘을 한 묶음에 넣으면
# "12월 5일까지 … 30일로 바꾼다" 같은 가장 흔한 확정 문장이 전부 날짜 충돌로 잡힌다.
_DATE_EXPR = re.compile(
    r"\d{1,2}\s*월\s*\d{1,2}\s*일"
    r"|\d{1,2}\s*월(?=\s*(?:까지|에|부터|,|$))"
    r"|\d{1,2}\s*일(?=\s*(?:까지|에|부터|,|$))"
    r"|[월화수목금토일]요일"
    r"|\d+\s*주\s*(?:뒤|후|내)?"
)
# 시각은 날짜와 따로 센다. "금요일 18시까지"는 기한 하나이므로, 둘을 한 묶음으로 세면
# 확정 문장을 스캔할 때 가장 자연스러운 기한 표현이 전부 충돌로 잡힌다.
_TIME_EXPR = re.compile(r"\d{1,2}\s*시(?:\s*\d{1,2}\s*분)?")
# '차'(1차·2차는 회차)는 세지 않고, "3개월"의 "3개"도 수량이 아니다.
_QUANTITY_EXPR = re.compile(r"\d+\s*(?:개(?!월)|명|건|퍼센트|%)")

# LLM 키가 없을 때 쓰는 고정 응답. 새 스키마 그대로여서 가짜 모드에서도 진행자 화면이
# 실제와 같은 모습으로 돈다(모호 2건 + 명확 1건). 근거 문장도 담아 화면에 "근거: …"가
# 보이게 한다 — 가짜 모드는 전사 원문 없이 파싱하므로(compile_specs) 근거 검증은 건너뛴다.
FAKE_DECISIONS = [
    {
        "topic": "로그 보관 기간",
        "ambiguous": True,
        "why": "\"한 달 정도면 충분\"이 30일인지 90일인지 갈립니다",
        "candidates": [
            {"text": "민수가 12월 5일까지 로그 보관 기간을 30일로 바꾸고, 스테이징 로그가 30일 뒤 지워지는 것을 확인한다",
             "evidence": "로그는 일단 한 달 정도면 충분할 것 같은데요", "recommended": True},
            {"text": "민수가 12월 5일까지 로그 보관 기간을 90일로 바꾸고, 감사 로그 3개월 요건을 문서에 남긴다",
             "evidence": "감사 때문에 분기 단위로 봐야 한다고 들었어요"},
        ],
    },
    {
        "topic": "QA 범위",
        "ambiguous": True,
        "why": "\"담당 화면\"이 각자 만든 화면인지 배포에 포함된 화면 전체인지 갈립니다",
        "candidates": [
            {"text": "각 담당자가 12월 8일까지 본인이 만든 화면만 QA하고, 결과를 팀 채널에 한 줄로 남긴다",
             "evidence": "담당 화면은 각자 보는 게 빠를 것 같아요", "recommended": True},
            {"text": "영희가 12월 8일까지 이번 배포에 포함된 화면 전체를 QA하고, 발견한 문제를 이슈로 등록한다",
             "evidence": "이번 배포는 화면이 엮여 있어서 전체를 한 번 봐야 해요"},
        ],
    },
    {
        "topic": "배포 스크립트 정리",
        "ambiguous": False,
        "final": "민수가 금요일 18시까지 배포 스크립트를 정리하고, 스테이징에서 한 번 돌려 성공하는 것을 확인한다",
        # 명확 항목의 근거는 규칙 안전망이 스캔한다 — 모호어도, 서로 다른 날짜도 없어야 한다.
        "evidence": "배포 스크립트는 제가 금요일까지 정리하겠습니다",
    },
]


class DecisionCompileError(Exception):
    """LLM 응답을 재시도까지 하고도 결정으로 못 읽었을 때, 또는 저장 중에 같은 회의를 두 번
    컴파일한 것이 드러났을 때. 진행자 화면에 사유를 그대로 띄운다."""


# ---------------------------------------------------------------------------
# 규칙 안전망 — AI가 '명확'이라고 한 항목만 되짚는다. 같은 입력이면 항상 같은 사유가 나와야
# 하므로(재현성) LLM이 아니라 이쪽에서 규칙으로 센다.
# ---------------------------------------------------------------------------


def _date_tokens(text):
    return {re.sub(r"\s+", "", m.group(0)) for m in _DATE_EXPR.finditer(text)}


def _time_tokens(text):
    return {re.sub(r"\s+", "", m.group(0)) for m in _TIME_EXPR.finditer(text)}


def _quantity_tokens(text):
    return {re.sub(r"\s+", "", m.group(0)) for m in _QUANTITY_EXPR.finditer(text)}


def _text_warnings(text, where):
    """한 텍스트에서 모호어와 날짜·시각·숫자 충돌을 찾아 사유 문자열로 만든다.

    where는 사유 앞에 붙는 말("확정 문장" 또는 "원문")이다. 조사 변화를 피하려고 표현을
    문장 끝에 콜론으로 붙인다 — '나중에'·'쯤'처럼 끝 음절이 다른 표현을 같은 틀에 담기 위함.

    날짜·시각·숫자는 각각 따로 센다. 같은 묶음 안에 서로 다른 값이 둘 이상일 때만 충돌이다
    — "금요일 18시까지"는 기한 하나이지 충돌이 아니다.
    """
    found = ["%s에 모호한 표현이 있어요: '%s'" % (where, phrase)
             for phrase in AMBIGUOUS_PHRASES if phrase in text]
    # 라벨마다 조사가 달라서(날짜'가' / 시각'이' / 숫자'가') 문구를 따로 적어 둔다.
    for label, tokens in (("다른 날짜가", _date_tokens(text)), ("다른 시각이", _time_tokens(text)),
                          ("다른 숫자가", _quantity_tokens(text))):
        if len(tokens) >= 2:
            found.append("%s에 %s 섞여 있어요: %s"
                         % (where, label, ", ".join(sorted(tokens))))
    return found


def safety_warnings(final_text, evidence):
    """AI가 '명확'이라고 한 항목을 되짚는 규칙 안전망. 사유 목록을 돌려준다.

    두 가지를 본다.
    - 확정 문장: AI가 쓴 문장 자체에 모호함이 남아 있다.
    - 원문 근거: 원문은 모호한데 AI가 그것을 혼자 해소해 버렸다. 그 해소가 맞을 수도 있지만
      회의에서 정해지지 않은 것을 AI가 정한 셈이므로 진행자가 확인해야 한다.

    모호 항목은 이미 진행자에게 묻고 있으므로 이 함수를 부르지 않는다. 같은 사유가 양쪽에서
    나오면 확정 문장 쪽만 남긴다 — 진행자가 고쳐야 할 대상이 그 문장이기 때문이다.
    """
    found = _text_warnings(final_text, "확정 문장") + _text_warnings(evidence, "원문")
    seen = []
    phrases_taken = set()
    for warning in found:
        phrase = warning.rsplit(": ", 1)[-1]
        if phrase in phrases_taken:
            continue
        phrases_taken.add(phrase)
        seen.append(warning)
    return seen


# ---------------------------------------------------------------------------
# ① 컴파일
# ---------------------------------------------------------------------------


def _json_array(raw):
    """LLM 응답에서 JSON 배열만 떼어낸다. 코드블록 표시나 설명이 섞여 나와도 견딘다."""
    match = re.search(r"\[.*\]", raw, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, list) else None


def _review_hints(source):
    """전사본에서 모호 표현이 있는 문장만 추려 LLM이 놓치지 않게 짚어준다.

    강제로 모호 판정을 내리는 게 아니라 '검토 대상'으로만 넘긴다 — 결정으로 다룰지, 다룬다면
    ambiguous로 볼지는 LLM이 그대로 판단한다. 그래서 REFERENT_PHRASES처럼 일상 대화에 흔한
    표현을 섞어도 과도하게 틀어막는(false positive) 비용이 없다 — LLM이 '결정이 아니다'로
    걸러내면 그만이다.
    """
    sentences = re.split(r"(?<=[.!?])\s+", source.strip())
    hint_phrases = AMBIGUOUS_PHRASES + REFERENT_PHRASES
    return [s for s in sentences if any(phrase in s for phrase in hint_phrases)]


def _compile_prompt(source):
    hints = _review_hints(source)
    hint_block = (
        "\n전사본에서 아래 문장들은 모호한 표현이 있을 가능성이 높다 — 결정으로 다룰 내용인지, "
        "다룬다면 ambiguous로 볼지 하나씩 반드시 검토하라(검토 결과 결정이 아니면 넣지 않아도 "
        "된다):\n" + "\n".join("- %s" % s for s in hints) + "\n"
    ) if hints else ""
    return (
        "다음은 회의 녹음의 전사 원문이다. 이 회의에서 나온 '결정'을 찾아라. 결정마다 그것이 "
        "사람마다 다르게 이해될 수 있는지 판단하고, 아래 JSON 형식으로 답하라.\n"
        + hint_block +
        "\n다르게 이해될 수 있는 결정(ambiguous: true):\n"
        "  why에 \"왜 다르게 이해할 수 있나\"를 한 줄로 쓰되, 원인이 된 전사본 표현을 따옴표로 "
        "그대로 인용해라(예: \"'한 달 정도면 충분'이 30일인지 90일인지 갈립니다\"). 누가 틀렸다는 "
        "식으로 쓰지 마라.\n"
        "  candidates에 그 결정이 갈릴 수 있는 방향마다 '그대로 공개될 확정 문장'을 2~3개 쓴다.\n"
        "다르게 이해될 여지가 없는 결정(ambiguous: false):\n"
        "  final에 '그대로 공개될 확정 문장' 하나를 쓴다. candidates는 쓰지 않는다.\n\n"
        "확정 문장(final, candidates의 text)을 쓰는 규칙:\n"
        "- 그 문장이 팀원에게 그대로 공개된다. 누가·무엇을·언제까지·완료 기준이 문장 안에 "
        "들어가야 실행할 수 있다.\n"
        "- 전사본에 나오지 않은 내용을 넣지 마라. 전사본에서 담당자나 기한을 찾을 수 없으면 "
        "그 방향의 후보는 쓰지 마라.\n"
        "- \"~라는 뜻\", \"A일 수도 B일 수도\" 같은 해설체로 쓰지 마라. 실행할 문장으로 쓴다.\n"
        "- 어느 후보가 맞는지 쓰지 마라. 고르는 것은 회의 진행자다.\n"
        "- 결론을 다음으로 미루기로 한 안건(\"내일 다시 얘기\", \"일단 보류\" 등)은 둘 중 하나를 "
        "고른 것처럼 쓰지 마라. ambiguous: true로 두고, candidates에는 아직 정해지지 않았다는 "
        "사실 그대로를 반영해 쓴다.\n\n"
        "evidence 규칙: 그 문장의 근거가 된 전사본 원문 문장을 토씨 하나 틀리지 않고 그대로 "
        "복사해서 써라. 원문에 없는 문장을 쓰면 그 후보는 버려진다.\n"
        "recommended 규칙: 근거가 가장 강한 후보 하나에만 true를 붙인다.\n"
        "다르게 이해될 수 있는 결정이 많으면, 회의에서 가장 중요한 5개에만 ambiguous를 붙여라.\n\n"
        "반드시 아래 JSON 배열 형식으로만 답하라. 설명이나 코드블록 표시 없이 JSON만 출력하라:\n"
        '[{"topic": "안건 한 줄", "ambiguous": true, "why": "왜 갈릴 수 있나", '
        '"candidates": [{"text": "확정 문장", "evidence": "근거가 된 원문 문장", "recommended": true}]}, '
        '{"topic": "안건 한 줄", "ambiguous": false, "final": "확정 문장", "evidence": "근거가 된 원문 문장"}]\n\n'
        "전사본:\n" + source
    )


def _generate_parsed(prompt, parser, what_failed):
    """프롬프트를 보내고 파싱까지 성공할 때까지 최대 (1 + PARSE_RETRIES)번 시도한다.

    - 연결 실패(OSError)와 응답 언어 오류는 None을 돌려준다 → 호출자가 가짜 응답으로 대체.
    - 연결은 됐는데 형식이 재시도 후에도 안 맞으면 DecisionCompileError를 올린다 — 읽을 수
      있는 결정이 하나도 없으면 화면에 보여줄 게 없으므로 회의 단위로 실패시키고 진행자에게
      사유를 띄운다. 항목 하나의 형식이 틀린 경우는 parse_items가 그 항목만 비워서 넘긴다.
    """
    for _ in range(PARSE_RETRIES + 1):
        try:
            raw = services._generate_clean(prompt)
        except OSError:
            return None
        if raw is None:
            return None
        parsed = parser(raw)
        if parsed:
            return parsed
    raise DecisionCompileError(
        "AI 응답을 %s로 읽지 못했습니다. 잠시 뒤 다시 시도해 주세요." % what_failed
    )


def compile_specs(source):
    """전사 원문 → [응답 항목, ...]. LLM이 없거나 연결이 안 되면 가짜 응답을 돌려준다."""
    if not (services._enabled() and source):
        return parse_items(json.dumps(FAKE_DECISIONS))
    items = _generate_parsed(
        _compile_prompt(source), lambda raw: parse_items(raw, source_text=source), "결정",
    )
    if items is None:
        return parse_items(json.dumps(FAKE_DECISIONS))
    return items


# ---------------------------------------------------------------------------
# 응답 파서
# ---------------------------------------------------------------------------


# 안건 이름이 길면 진행자 화면의 카드 제목이 레이아웃을 깨므로 여기서 자른다.
MAX_TOPIC_LENGTH = 120


def _parse_candidates(raw_candidates, normalized_source):
    """후보 배열 → [{"text","evidence","recommended"}, ...].

    - 객체가 아닌 항목(문자열만 온 경우)은 조용히 버린다. 한 항목의 형식이 틀렸다고 회의
      전체를 실패시키지 않는다.
    - 근거가 원문에 없는 후보는 버린다. evidence가 빈 문자열이면 검증을 건너뛴다
      (가짜 응답 모드에는 대조할 원문이 없다).
    - 추천은 처음 하나만 인정한다 — 화면의 'AI 추천' 배지는 하나여야 읽는 순서가 생긴다.
    """
    results = []
    recommended_taken = False
    for item in raw_candidates if isinstance(raw_candidates, list) else []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        evidence = str(item.get("evidence", "")).strip()
        if normalized_source is not None and evidence and services._normalize_for_match(evidence) not in normalized_source:
            continue
        if any(text == kept["text"] for kept in results):
            continue
        recommended = bool(item.get("recommended")) and not recommended_taken
        recommended_taken = recommended_taken or recommended
        results.append({"text": text, "evidence": evidence, "recommended": recommended})
        if len(results) >= MAX_INTERPRETATIONS:
            break
    return results


def parse_items(raw, source_text=None):
    """LLM 응답 → [{"topic","ambiguous","why","final","evidence","candidates"}, ...].

    한 번의 호출로 받은 응답을 전부 여기서 읽는다. 항목 하나의 형식이 틀렸다고 나머지를
    버리지 않는다 — 읽을 수 없는 조각만 비워서 넘기고, 그 항목은 진행자가 직접 쓰거나 다음
    회의로 넘기게 된다.

    - topic이 비면 그 항목은 결정이 될 수 없으므로 버린다.
    - 모호 항목(ambiguous=True)은 candidates만 쓰고 final/evidence를 버린다. 반대로 명확
      항목은 final/evidence만 쓰고 candidates를 버린다 — 공개 기준이 둘이 되지 않게.
    - source_text가 주어지면 근거를 원문과 대조한다. 명확 항목의 근거가 원문에 없으면
      final을 비워 돌려주고(호출자가 '확인 필요'로 올린다), 후보는 그 후보만 버린다.
    - 근거 문장이 겹치는 항목은 중복으로 보고 먼저 나온 것만 남긴다(_dedupe_items) — 같은
      전사 문장을 topic만 바꿔 두 번 만들어내는 LLM 응답을 걸러낸다.
    """
    data = _json_array(raw)
    if data is None:
        return []
    normalized_source = services._normalize_for_match(source_text) if source_text is not None else None
    items = []
    for row in data:
        if not isinstance(row, dict):
            continue
        topic = str(row.get("topic", "")).strip()[:MAX_TOPIC_LENGTH]
        if not topic:
            continue
        ambiguous = bool(row.get("ambiguous"))
        final = "" if ambiguous else str(row.get("final", "")).strip()
        evidence = "" if ambiguous else str(row.get("evidence", "")).strip()
        # 원문을 들고 검증하는 중이라면, 근거가 없는 확정 문장은 "원문에 있다"고 볼 수 없다.
        # (후보 쪽의 빈 근거는 가짜 응답 모드 통로로 남긴다 — _parse_candidates 참고.)
        if final and normalized_source is not None and (
            not evidence or services._normalize_for_match(evidence) not in normalized_source
        ):
            final = ""
        items.append({
            "topic": topic,
            "ambiguous": ambiguous,
            "why": str(row.get("why", "")).strip() if ambiguous else "",
            "final": final,
            "evidence": evidence,
            "candidates": _parse_candidates(row.get("candidates"), normalized_source) if ambiguous else [],
        })
        if len(items) >= MAX_DECISIONS:
            break
    return _dedupe_items(items)


def _dedupe_items(items):
    """topic과 근거 문장이 둘 다 같은 항목은 중복 추출로 보고 먼저 나온 것만 남긴다.

    근거만 보고 합치면 위험하다 — 한 문장에서 서로 다른 결정 두 개가 나올 수 있다(예: "민수가
    로그는 30일로 줄이고 QA는 다음 주까지 진행합니다"는 '로그 보관 기간'과 'QA 일정' 둘 다의
    근거일 수 있다). topic만 보고 합쳐도 위험하다 — 우연히 같은 이름을 쓴 서로 다른 안건을
    잃을 수 있다. 그래서 둘 다 같을 때만(같은 안건을 문구만 살짝 바꿔 두 번 만들어낸 경우)
    중복으로 본다. 더 좁게 잡는 대신, 서로 다른 안건을 잘못 합치는 사고를 내지 않는다.
    """
    seen_evidence_by_topic = {}
    deduped = []
    for item in items:
        evidences = (
            {services._normalize_for_match(c["evidence"]) for c in item["candidates"] if c["evidence"]}
            if item["ambiguous"] else
            ({services._normalize_for_match(item["evidence"])} if item["evidence"] else set())
        )
        prior = seen_evidence_by_topic.get(item["topic"])
        if evidences and prior and evidences & prior:
            continue
        seen_evidence_by_topic[item["topic"]] = (prior or set()) | evidences
        deduped.append(item)
    return deduped


# ---------------------------------------------------------------------------
# ①+② 한 번에 — 진행자가 "결정 컴파일 실행"을 눌렀을 때 들어오는 입구
# ---------------------------------------------------------------------------


def compile_meeting(meeting):
    """회의 전사 원문을 컴파일해 Decision/Interpretation을 저장하고 요약을 돌려준다.

    {"decisions": n, "ambiguous": n, "few_candidates": n}
    - ambiguous: 진행자가 확인해야 하는 항목 수(AI가 갈린다고 봤거나 규칙이 걸린 것)
    - few_candidates: 확인이 필요한데 후보가 하나도 없는 항목 수. 근거 검증에서 후보가 전부
      버려졌거나 AI가 후보를 못 만든 경우다 — 진행자는 직접 쓰기나 '다음 회의로 넘김'으로만
      처리할 수 있다.

    이미 Decision이 있는 회의는 건드리지 않는다(공개 뒤에 결정이 바뀌면 팀원이 확인한 내용과
    달라진다). 다시 컴파일하려면 호출 전에 draft 상태인 Decision을 지워야 한다.
    """
    if meeting.decisions.exists():
        raise DecisionCompileError("이미 컴파일된 회의입니다. 다시 하려면 기존 결정을 먼저 지워 주세요.")
    source = meeting.transcript
    items = compile_specs(source)
    if not items:
        raise DecisionCompileError("전사 원문에서 공개할 결정을 찾지 못했습니다.")

    ambiguous_count = 0
    few_candidates = 0
    try:
        ambiguous_count, few_candidates = _save_items(meeting, items)
    except IntegrityError:
        # 진행자가 컴파일 버튼을 두 번 눌렀을 때. 위의 exists() 검사는 LLM 호출(수 초) 앞에
        # 있으므로 두 요청이 모두 빈 상태를 보고 통과해 같은 order를 쓰려 한다. 먼저 저장한
        # 쪽의 결과를 그대로 두고, 늦게 온 쪽에는 사유를 보여준다(500 대신).
        raise DecisionCompileError("이미 컴파일된 회의입니다. 화면을 새로 고쳐 주세요.")
    return {
        "decisions": len(items),
        "ambiguous": ambiguous_count,
        "few_candidates": few_candidates,
    }


def _looks_deferred(item):
    """결론을 다음으로 미루기로 한 근거 문장인가(DEFERRAL_PHRASES).

    모호 항목은 후보들의 근거를, 명확 항목은 그 근거 하나를 본다 — 둘 다 전사 원문에서 가져온
    텍스트라 AI가 쓴 topic/why보다 믿을 수 있다.
    """
    texts = [c["evidence"] for c in item["candidates"]] if item["ambiguous"] else [item["evidence"]]
    return any(phrase in text for text in texts for phrase in DEFERRAL_PHRASES)


def _save_items(meeting, items):
    """응답 항목들을 Decision/Interpretation으로 저장하고 (확인 필요 수, 후보 없는 수)를 돌려준다."""
    ambiguous_count = 0
    few_candidates = 0
    with transaction.atomic():
        for order, item in enumerate(items):
            warnings, divergence_reason, final_text, candidates = _resolve_item(item)
            # 미루기로 한 안건은 AI가 뭐라고 썼든 '다음 회의로 넘김'을 기본값으로 건다 —
            # 진행자가 수동으로 고르는 것과 똑같이 final_text/marked_undecided를 같이 채운다
            # (views._save_decision_edits의 CHOICE_UNDECIDED 처리와 동일).
            deferred = _looks_deferred(item)
            decision = Decision.objects.create(
                meeting=meeting, order=order, what=item["topic"],
                warnings=warnings, divergence_reason=divergence_reason,
                final_text=undecided_sentence(item["topic"]) if deferred else final_text,
                marked_undecided=deferred,
            )
            for candidate_order, candidate in enumerate(candidates):
                Interpretation.objects.create(
                    decision=decision, order=candidate_order,
                    text=candidate["text"], evidence=candidate["evidence"],
                    recommended=candidate["recommended"],
                )
            if decision.has_warning:
                ambiguous_count += 1
                if not candidates:
                    few_candidates += 1
    return ambiguous_count, few_candidates


def _resolve_item(item):
    """응답 항목 하나 → (warnings, divergence_reason, final_text, candidates).

    세 갈래다.
    1. AI가 모호하다고 본 항목: AI의 이유와 후보를 그대로 쓴다. 규칙은 부르지 않는다 —
       이미 진행자에게 묻고 있으므로 중복 검사할 이유가 없다.
    2. AI가 명확하다고 했지만 근거를 원문에서 확인하지 못한 항목(parse_items가 final을
       비운 경우): 확인 필요로 올리고 후보는 두지 않는다.
    3. AI가 명확하다고 하고 근거도 확인된 항목: 규칙 안전망을 돌린다. 걸리면 확인 필요로
       올리고 AI가 쓴 문장을 "이 문장 그대로 공개" 후보 하나로 보여준다. 걸리지 않으면 그
       문장이 곧 확정 내용이고 진행자는 탭하지 않는다.
    """
    if item["ambiguous"]:
        # AI가 이유를 비워 보낼 수 있다. 사유가 비면 has_warning이 False가 되어 모호한 결정이
        # 확인 없이 공개되므로, 기본 문구로라도 채워 확인 대상으로 남긴다.
        return [], item["why"] or "AI가 다르게 이해할 수 있다고 봤어요", "", item["candidates"]
    if not item["final"]:
        return [], "근거를 원문에서 확인하지 못했어요", "", []
    warnings = safety_warnings(item["final"], item["evidence"])
    if not warnings:
        return [], "", item["final"], []
    return (
        warnings, "", "",
        [{"text": item["final"], "evidence": item["evidence"], "recommended": False}],
    )


# ---------------------------------------------------------------------------
# ③ 진행자 확정 + ④ 확정 문장 공개
#
# 진행자는 확인이 필요한 항목마다(Decision.needs_host) [후보 선택 | 직접 쓰기 | 다음 회의로
# 넘김 | 이 결정 빼기] 중 하나를 고른다. 확인이 필요 없는 항목은 AI가 쓴 문장이 곧 확정
# 내용이라 고를 것이 없고(접힌 '고치기'로 바꿀 수는 있다), 공개 대상에는 똑같이 포함된다.
# 공개를 막는 조건은 하나다 — 공개할 문장이 있는가(Decision.blocks_publish).
# ---------------------------------------------------------------------------

# 진행자 확정 라디오 버튼 value. "candidate:12" 형태.
CHOICE_CANDIDATE_PREFIX = "candidate:"
CHOICE_CUSTOM = "custom"
CHOICE_UNDECIDED = "undecided"
# AI가 회의에 없던 결정을 만들어 냈을 때 그 항목을 지운다. '다음 회의로 넘김'과 달라야 하는
# 이유: 없던 안건을 이월하면 다음 회의마다 계속 보이게 된다.
CHOICE_DROP = "drop"


def undecided_sentence(topic):
    """'다음 회의로 넘김'을 고른 항목의 확정 문장.

    미결정 항목도 문장을 가져야 한다 — 공개되는 것은 어떤 상태에서나 final_text 하나이고,
    팀원 화면과 다음 회의의 이월 목록이 이 문장을 그대로 쓴다. 진행자는 탭 한 번만 하고
    문장은 서버가 만든다.
    """
    return "%s — 이번 회의에서 결론이 나지 않았음 (다음 회의로)" % topic


def publish_blockers(decisions):
    """공개를 막는 항목 — 확인이 필요한데 진행자가 아직 고르지 않은 것, 그리고 문장이 빈 것.

    이 목록이 비어 있어야 "확정 명세 공개"를 누를 수 있다. 화면의 버튼 비활성화와 서버의
    거부가 같은 판정을 쓰도록 여기 한 곳에서만 센다.

    문장 유무를 함께 보는 이유: 팀원 화면은 final_text 하나만 보여주므로 문장이 없으면 번호만
    있는 빈 줄이 공개된다. AI가 모호 판정을 하면서 이유를 비워 보내거나, 이번 재설계 전에
    만들어진 무경고 draft가 그대로 넘어오는 경로가 있어 마지막 그물을 여기 둔다.
    """
    return [decision for decision in decisions if decision.blocks_publish]


def publish_decisions(meeting):
    """draft 항목을 공개로 넘긴다. 호출 전에 publish_blockers()가 비어 있어야 한다.

    '아직 미정'으로 넘긴 항목은 undecided가 되어 다음 회의로 이월되고(Phase D), 나머지는
    경고 유무와 무관하게 published가 되어 팀원 확인 대상이 된다.
    (공개된 수, 미결정으로 넘어간 수) 를 돌려준다.
    """
    drafts = list(meeting.decisions.filter(status=Decision.STATUS_DRAFT))
    undecided_ids = [decision.pk for decision in drafts if decision.marked_undecided]
    published_ids = [decision.pk for decision in drafts if not decision.marked_undecided]
    with transaction.atomic():
        if undecided_ids:
            Decision.objects.filter(pk__in=undecided_ids).update(status=Decision.STATUS_UNDECIDED)
        if published_ids:
            Decision.objects.filter(pk__in=published_ids).update(status=Decision.STATUS_PUBLISHED)
    return len(published_ids), len(undecided_ids)


def revise_final_text(decision, text):
    """공개된 항목의 확정 문장을 고친다.

    그 항목의 확인 기록(DecisionAgreement)을 모두 지우고 revised를 세운다 — 팀원이 확인한
    문장과 내용이 달라졌으므로 다시 받아야 하고, revised가 있어야 팀원 화면에서 "처음 보는
    항목"과 "내용이 바뀐 항목"을 구별해 안내할 수 있다.
    지워진 확인 기록 수를 돌려준다.
    """
    with transaction.atomic():
        Decision.objects.filter(pk=decision.pk).update(final_text=text, revised=True)
        removed = decision.agreements.all().delete()[0]
    decision.final_text = text
    decision.revised = True
    return removed


def published_decisions(meeting):
    """팀원에게 보여줄 항목 — 공개된 것과 미결정으로 넘어간 것.

    draft는 절대 포함되지 않는다. 공개 전에는 팀원 화면에 확정 명세가 아예 담기지 않아야
    하므로, 팀원 쪽 조회는 반드시 이 함수를 거친다.
    """
    return list(
        meeting.decisions
        .filter(status__in=[Decision.STATUS_PUBLISHED, Decision.STATUS_UNDECIDED])
        .prefetch_related("agreements")
    )


def ack_counts(decisions):
    """진행자 화면용 공개 항목별 {decision_id: {"ack": n, "differ": m, "comments": [...]}}.

    user는 아예 조회하지 않는다 — 누가 '달랐어요'를 골랐는지, 의견을 누가 썼는지는 결과 어디에도
    담기지 않는다. 미결정 항목은 읽기 전용이라 세지 않는다.
    """
    published_ids = [decision.id for decision in decisions if decision.status == Decision.STATUS_PUBLISHED]
    counts = {decision_id: {"ack": 0, "differ": 0, "comments": []} for decision_id in published_ids}
    rows = DecisionAgreement.objects.filter(decision_id__in=published_ids).values_list("decision_id", "kind", "comment")
    for decision_id, kind, comment in rows:
        row = counts[decision_id]
        if kind == DecisionAgreement.KIND_DIFFER:
            row["differ"] += 1
            if comment:
                row["comments"].append(comment)
        else:
            row["ack"] += 1
    for row in counts.values():
        # 저장 순서대로 보여주면 시점으로 작성자를 짐작할 수 있어서 가나다순으로 섞는다.
        row["comments"].sort()
    return counts


# ---------------------------------------------------------------------------
# ④ 미결정 이월
# ---------------------------------------------------------------------------


def carryover_decisions(meeting):
    """이 회의보다 먼저 만들어진 같은 팀 회의의 미결정 안건 중 아직 '처리됨'이 안 된 것.

    진행자가 '처리됨'을 누를 때까지(carryover_resolved_at이 채워질 때까지) 이후 회의마다
    계속 보인다 — 한 번 보고 사라지면 묻히기 때문이다.
    """
    return list(
        Decision.objects
        .filter(
            meeting__team_id=meeting.team_id,
            meeting__created_at__lt=meeting.created_at,
            status=Decision.STATUS_UNDECIDED,
            carryover_resolved_at__isnull=True,
        )
        .select_related("meeting")
        .order_by("meeting__created_at", "order", "id")
    )
