"""결정 사항 추출 기능(실험 기능) — 확정 문장 후보를 만들고 진행자가 고르게 한다.

AI를 한 번 부른다. 녹취 원문을 보내고 결정마다
  [안건 한 줄 · 모호 여부 · 왜 갈리나 · 원문 근거 · AI가 가장 추천하는 확정 문장 하나]
을 한 응답으로 받는다(parse_items).

- 모호한 결정: AI가 "왜 다르게 이해할 수 있나" 한 줄과 그대로 공개될 확정 문장을 가장 추천하는
  것 하나만 만든다(MAX_INTERPRETATIONS = 1). 생성한 문장에는 원문 근거를 따로 붙이지 않는다 —
  원문 근거는 결정 하나에 하나(Decision.evidence)이고 녹취 원문 창의 하이라이트에만 쓴다.
  추천 문장을 미리 선택해 두지는 않는다 — 진행자가 읽고 직접 골라야 모호함이 실제로 정리되기
  때문이다. 진행자가 아무것도 고르지 않고 공개하면 그 결정은 빠진다(views._save_decision_edits).
- 명확한 결정: AI가 확정 문장 하나를 쓰고, 진행자 탭 없이 공개 목록에 들어간다.
- 규칙 안전망(safety_warnings): AI가 '명확'이라 한 항목만 되짚는다. 확정 문장이나 원문 근거에
  모호어·날짜·숫자 충돌이 있거나 확정 문장에 "그쪽/그분" 같은 지시어가 남아 있으면 확인
  필요로 올리고, AI가 쓴 문장을 후보 하나로 보여준다.
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
       공백을 모두 지운 비교용 문자열. 근거 문장이 녹취 원문에 있는지 볼 때 쓴다.
"""

import json
import re

from django.db import IntegrityError, transaction

from . import services
from .models import Decision, Interpretation

# 한 회의에서 받을 결정 수 상한(LLM이 회의록을 잘게 쪼개는 경우 방지).
MAX_DECISIONS = 12
# 결정 하나에 보여줄 AI 확정 문장 수. 가장 추천하는 하나만 보여준다 — 후보 여러 개를 읽고 비교하는
# 것이 진행자에게 가장 큰 부담이었고, 고르지 않는 것은 '직접 쓰기'로 언제든 대신할 수 있다.
MAX_INTERPRETATIONS = 1
# 파싱 실패 시 같은 프롬프트로 한 번 더 보내고, 그래도 안 되면 실패 처리한다.
PARSE_RETRIES = 1
# 확인이 필요한 결정 수에는 상한을 두지 않는다. 모호 판정이 AI로 넘어간 뒤에 서버가 앞쪽
# 몇 개만 남기면, 모호하다고 판정된 항목이 진행자 동의 없이 조용히 공개되거나 자동으로
# 이월된다. 상한은 프롬프트로만 유도한다(_compile_prompt).

# 확정 문장에 섞이면 사람마다 다르게 읽는 표현. 팀에서 합의한 목록이라 자동 추출이 아니라 고정 목록이다.
AMBIGUOUS_PHRASES = ("쯤", "시간 되면", "각자", "나중에", "아까 정한 대로", "적당히", "대충")

# 지시어·대명사. 두 곳에서 쓰지만 대상이 다르다.
# - _review_hints: 전사본 원문(자연스러운 대화체)을 훑어 "이 문장은 결정으로 다룰지 검토
#   하라"고 LLM에 짚어주기만 한다 — 대화체엔 이 표현이 워낙 흔해서, 바로 확인 필요로 만들면
#   '명확' 판정을 과도하게 뒤집어 버린다.
# - safety_warnings: AI가 직접 쓴 확정 문장(원문이 아니다)에서만 검사한다. 그대로 공개될
#   문장에 지시어가 남아 있으면 실제로 담당자·대상을 못 정한 것이라 과탐 위험이 훨씬 낮다.
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
# 보이게 한다 — 가짜 모드는 녹취 원문 없이 파싱하므로(compile_specs) 근거 검증은 건너뛴다.
FAKE_DECISIONS = [
    {
        "topic": "로그 보관 기간",
        "ambiguous": True,
        "why": "\"한 달 정도면 충분\"이 30일인지 90일인지 갈립니다",
        "evidence": "로그는 일단 한 달 정도면 충분할 것 같은데요",
        "candidate": "민수가 12월 5일까지 로그 보관 기간을 30일로 바꾸고, 스테이징 로그가 30일 뒤 지워지는 것을 확인한다",
    },
    {
        "topic": "QA 범위",
        "ambiguous": True,
        "why": "\"담당 화면\"이 각자 만든 화면인지 배포에 포함된 화면 전체인지 갈립니다",
        "evidence": "담당 화면은 각자 보는 게 빠를 것 같아요",
        "candidate": "각 담당자가 12월 8일까지 본인이 만든 화면만 QA하고, 결과를 팀 채널에 한 줄로 남긴다",
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
    추출한 것이 드러났을 때. 진행자 화면에 사유를 그대로 띄운다."""


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


def _text_warnings(text, where, phrases=AMBIGUOUS_PHRASES):
    """한 텍스트에서 모호어와 날짜·시각·숫자 충돌을 찾아 사유 문자열로 만든다.

    where는 사유 앞에 붙는 말("확정 문장" 또는 "원문")이다. 조사 변화를 피하려고 표현을
    문장 끝에 콜론으로 붙인다 — '나중에'·'쯤'처럼 끝 음절이 다른 표현을 같은 틀에 담기 위함.
    phrases는 검사할 고정 표현 목록으로, 기본은 AMBIGUOUS_PHRASES다(아래 safety_warnings
    참고 — 확정 문장에는 REFERENT_PHRASES도 더해 검사한다).

    날짜·시각·숫자는 각각 따로 센다. 같은 묶음 안에 서로 다른 값이 둘 이상일 때만 충돌이다
    — "금요일 18시까지"는 기한 하나이지 충돌이 아니다.
    """
    found = ["%s에 모호한 표현이 있어요: '%s'" % (where, phrase)
             for phrase in phrases if phrase in text]
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
    - 확정 문장: AI가 쓴 문장 자체에 모호함이 남아 있다. "그쪽/그분/그거" 같은 지시어
      (REFERENT_PHRASES)도 여기서만 같이 본다 — AI가 직접 쓴, 그대로 공개될 문장에 누구·
      무엇인지 안 정해진 표현이 남아 있으면 실제로 못 고친 것이다("담당자는 … 해당 분에게
      넘긴다" 처럼).
    - 원문 근거: 원문은 모호한데 AI가 그것을 혼자 해소해 버렸다. 그 해소가 맞을 수도 있지만
      회의에서 정해지지 않은 것을 AI가 정한 셈이므로 진행자가 확인해야 한다. 원문은
      REFERENT_PHRASES를 보지 않는다 — 자연스러운 대화체엔 지시어가 흔히 섞여서, 거기까지
      걸면 멀쩡한 '명확' 판정을 과도하게 뒤집는다.

    모호 항목은 이미 진행자에게 묻고 있으므로 이 함수를 부르지 않는다. 같은 사유가 양쪽에서
    나오면 확정 문장 쪽만 남긴다 — 진행자가 고쳐야 할 대상이 그 문장이기 때문이다.
    """
    found = (
        _text_warnings(final_text, "확정 문장", AMBIGUOUS_PHRASES + REFERENT_PHRASES)
        + _text_warnings(evidence, "원문")
    )
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
# ① 추출
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
    """'결정'만 찾게 하면 "최대한 빨리 할게요" 같은 약속이나 "이따 단톡에서 정해요"로 미룬 안건이 검사 대상에서
    빠지고, '다르게 이해될 수 있나'만 물으면 미룬 것을 '미루기로 확정'으로 읽는다. 그래서 실행 항목을 빠짐없이 뽑게
    한 뒤 점검표로 하나씩 대조하게 한다. 애매한 결정 4개·5개를 심은 합성 회의 두 개에서 기존 프롬프트는 4개 중
    1개를 찾았고, 이 프롬프트는 9개를 모두 찾았다(gemini-3.5-flash, 호출 횟수는 같음). 7번은 점검표에 없는 애매함
    (말뜻 차이, 지시 대상, 완료 기준, 엇갈린 발언, 조건부 결정)을 놓치지 않게 하는 마지막 그물이다.

    여기에 규칙으로 찾은 모호 표현 문장 목록(_review_hints)을 붙여 반드시 검토하게 하고, why에는 원인이 된 원문
    표현을 인용하게 하며, 결론을 미룬 안건은 하나를 고른 것처럼 쓰지 않게 한다(PR #44).
    """
    hints = _review_hints(source)
    hint_block = (
        "\n전사본에서 아래 문장들은 모호한 표현이 있을 가능성이 높다 — 실행 항목으로 다룰 내용인지, "
        "다룬다면 ambiguous로 볼지 하나씩 반드시 검토하라(검토 결과 실행 항목이 아니면 넣지 않아도 "
        "된다):\n" + "\n".join("- %s" % s for s in hints) + "\n"
    ) if hints else ""
    return (
        "다음은 회의 녹음의 녹취 원문이다. 회의가 끝난 뒤 팀원들이 각자 움직이려면 무엇을 알아야 하는지 기준으로 "
        "이 회의의 '실행 항목'을 빠짐없이 뽑고, 항목마다 팀원들이 다르게 이해하거나 다르게 행동할 수 있는지 "
        "판단하라.\n"
        + hint_block +
        "\n실행 항목에 넣을 것(하나도 빠뜨리지 마라):\n"
        "- 확정된 결정 (예: 주제, 역할 분담)\n"
        "- 누군가 하겠다고 한 일 (예: \"자료는 제가 찾아볼게요\", \"단톡방은 제가 만들게요\")\n"
        "- 정하려다 뒤로 미룬 것 (예: \"그건 이따 단톡에서 정해요\", \"나중에 다시 얘기해요\")\n"
        "- 다음 회의의 날짜·시간·장소\n"
        "인사, 자기소개, 진행 멘트는 넣지 마라.\n\n"
        "항목마다 아래 점검표를 원문과 하나씩 맞춰 보고, 하나라도 해당하면 ambiguous: true다.\n"
        "1. 기한이 없거나 막연하다: \"최대한 빨리\", \"되는 대로\", \"그때\", \"나중에\", \"쯤\", \"다음 주쯤\" 등\n"
        "2. 정하지 않고 미뤘다: \"단톡에서 정해요\", \"이따 정해요\", \"봐서 정해요\" — 정할 때까지 팀원마다 "
        "다르게 준비한다\n"
        "3. 담당자나 범위가 불분명하다: \"각자\", \"편한 대로\", \"누가 하나\", 어디까지 하는지 안 정함\n"
        "4. 선택지가 둘 이상 남았다: \"A 아니면 B\"\n"
        "5. 회의 앞부분에 나온 제약과 충돌한다: 누군가 말한 불가능한 시간·사정이 나중에 정한 일정과 겹치는데 "
        "아무도 짚지 않았다. 원문 전체에서 각자 말한 가능/불가능 시간을 먼저 모아 두고 대조하라.\n"
        "6. 대답이 확답이 아니다: \"일단 알겠어요\", \"아마도\", \"해 볼게요\"\n"
        "7. 그 밖에 위에 없더라도, 팀원이 회의를 마치고 각자 다르게 이해하거나 다르게 행동할 수 있는 점: 같은 말을 "
        "다르게 받아들일 수 있다(예: \"한 장 정도\"가 슬라이드인지 문서인지), 가리키는 대상이 불분명하다(\"아까 그거\"), "
        "완료 기준이 없다, 사람마다 말한 내용이 엇갈렸는데 정리하지 않았다, 조건이 풀려야 확정되는 결정이다(\"면접 "
        "없으면 금요일\"). 7번은 실제로 행동이 갈릴 때만 붙이고, 말투만 다른 정도로는 붙이지 마라.\n"
        "1~7 중 아무것도 해당하지 않으면 ambiguous: false다.\n\n"
        "ambiguous: true 항목:\n"
        "  why에 원문에서 무엇이 빠졌거나 부딪혀서 어떻게 다르게 이해할 수 있는지를 한 줄로 구체적으로 쓰되, 원인이 된 "
        "전사본 표현을 따옴표로 그대로 인용해라(예: \"'한 달 정도면 충분'이 30일인지 90일인지 갈립니다\"). 점검표 "
        "번호(\"1번에 해당\" 등)는 쓰지 마라 — 진행자 화면에 그대로 보인다. 누가 틀렸다는 식으로 쓰지 마라.\n"
        "  candidate에 진행자에게 가장 추천하는 '그대로 공개될 확정 문장' 하나만 쓴다. 빠진 부분을 원문 흐름상 "
        "가장 그럴듯한 쪽으로 채운 실행 문장이다. 원문에 없는 날짜·이름을 지어내지 말고, 원문에 나온 날짜·요일·"
        "시간·장소 안에서만 고른다. 채울 근거가 원문에 전혀 없으면 \"~은 다음 회의 전까지 단톡방에서 정한다\"처럼 "
        "정하는 방법을 확정하는 문장을 쓴다.\n"
        "ambiguous: false 항목:\n"
        "  final에 '그대로 공개될 확정 문장' 하나를 쓴다. candidate는 쓰지 않는다.\n\n"
        "확정 문장(final, candidates의 text) 규칙: 그 문장이 팀원에게 그대로 공개된다. 누가·무엇을·언제까지가 "
        "문장 안에 들어간 실행 문장으로 쓴다. \"~라는 뜻\", \"A일 수도 B일 수도\" 같은 해설체로 쓰지 마라. 어느 "
        "쪽이 맞는지 해설하지 마라 — 확정하는 것은 회의 진행자다.\n"
        "결론을 다음으로 미루기로 한 안건(\"내일 다시 얘기\", \"일단 보류\" 등)은 둘 중 하나를 고른 것처럼 쓰지 "
        "마라. ambiguous: true로 두고, candidate는 아직 정해지지 않았다는 사실 그대로를 반영해 쓴다.\n"
        "evidence 규칙: 항목마다 그 결정이 나온 전사본 원문 구절을 토씨 하나 틀리지 않고 그대로 복사해서 써라(짧게 "
        "한 구절). 확정 문장의 근거가 아니라 회의 원문에서 이 안건이 나온 자리다.\n"
        "ambiguous 항목이 많으면, 회의에서 가장 중요한 5개에만 ambiguous를 붙여라.\n\n"
        "반드시 아래 JSON 배열 형식으로만 답하라. 설명이나 코드블록 표시 없이 JSON만 출력하라:\n"
        '[{"topic": "안건 한 줄", "ambiguous": true, "why": "왜 갈릴 수 있나", '
        '"evidence": "이 안건이 나온 원문 문장", "candidate": "가장 추천하는 확정 문장"}, '
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
    """녹취 원문 → [응답 항목, ...]. LLM이 없거나 연결이 안 되면 가짜 응답을 돌려준다."""
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


def _draft_hint_text(text):
    """모호어·지시어가 남은 문장에서 그 부분만 빈칸 표시로 바꾼다.

    담당자·기한처럼 AI가 이미 정한 부분은 그대로 살리고, 끝내 못 정한 부분만 빈칸으로
    남겨서 '직접 쓰기' 칸의 출발점으로 쓴다 — 진행자가 그 자리만 채우면 되게 한다.
    """
    for phrase in AMBIGUOUS_PHRASES + REFERENT_PHRASES:
        text = text.replace(phrase, "(구체적으로 적기)")
    return text


def _parse_candidates(raw_candidates):
    """AI 추천 문장 → ([{"text","evidence","recommended"}] 최대 MAX_INTERPRETATIONS개, draft_hint).

    새 형식은 문자열 하나("candidate")지만, 예전 형식처럼 후보 배열("candidates")이 와도 읽는다 —
    모델이 지시를 어기고 여러 개를 보내는 일이 흔하다. 그때는 recommended가 붙은 것을 먼저,
    나머지는 온 순서대로 보고 쓸 수 있는 첫 문장 하나만 남긴다.

    - 객체도 문자열도 아닌 항목은 조용히 버린다. 한 항목의 형식이 틀렸다고 회의 전체를
      실패시키지 않는다.
    - 확정 문장에 모호어·지시어(AMBIGUOUS_PHRASES, REFERENT_PHRASES)가 그대로 남은 문장은
      고를 의미가 없으므로 버린다 — safety_warnings가 '명확' 항목의 확정 문장에 적용하는 것과
      같은 기준이다. 버리기 전에 그 문장을 draft_hint로 하나만 남겨 '직접 쓰기'의 출발점으로 쓴다.
    - 생성한 문장에는 원문 근거를 붙이지 않는다(evidence는 늘 빈 문자열). 근거 검증으로 문장을
      버리지도 않는다 — 근거는 결정 하나에 하나(item["evidence"])다.
    """
    if isinstance(raw_candidates, str):
        raw_candidates = [raw_candidates]
    entries = []
    for item in raw_candidates if isinstance(raw_candidates, list) else []:
        if isinstance(item, str):
            entries.append((item.strip(), False))
        elif isinstance(item, dict):
            entries.append((str(item.get("text", "")).strip(), bool(item.get("recommended"))))
    # 정렬이 안정적이라 추천끼리·비추천끼리는 온 순서가 유지된다.
    entries.sort(key=lambda entry: not entry[1])
    results = []
    draft_hint = ""
    for text, _ in entries:
        if not text:
            continue
        if any(phrase in text for phrase in AMBIGUOUS_PHRASES + REFERENT_PHRASES):
            if not draft_hint:
                draft_hint = _draft_hint_text(text)
            continue
        results.append({"text": text, "evidence": "", "recommended": True})
        if len(results) >= MAX_INTERPRETATIONS:
            break
    return results, draft_hint


def _legacy_candidate_evidence(raw_candidates, normalized_source):
    """예전 형식(후보마다 evidence)으로 답했을 때 결정의 근거로 쓸 첫 후보 근거.

    결정 단위 evidence가 비어 있을 때만 쓴다. 원문을 들고 있으면 원문에서 찾은 것만 쓴다 —
    근거는 녹취 원문 창 하이라이트와 '보류' 자동 감지(_looks_deferred)에 쓰이기 때문이다.
    """
    for item in raw_candidates if isinstance(raw_candidates, list) else []:
        if not isinstance(item, dict):
            continue
        evidence = str(item.get("evidence", "")).strip()
        if evidence and (normalized_source is None or services._normalize_for_match(evidence) in normalized_source):
            return evidence
    return ""


def parse_items(raw, source_text=None):
    """LLM 응답 → [{"topic","ambiguous","why","final","evidence","candidates","draft_hint"}, ...].

    한 번의 호출로 받은 응답을 전부 여기서 읽는다. 항목 하나의 형식이 틀렸다고 나머지를
    버리지 않는다 — 읽을 수 없는 조각만 비워서 넘기고, 그 항목은 진행자가 직접 쓰거나 다음
    회의로 넘기게 된다.

    - topic이 비면 그 항목은 결정이 될 수 없으므로 버린다.
    - 모호 항목(ambiguous=True)은 AI 추천 문장(candidates, 최대 1개)만 쓰고 final을 버린다.
      반대로 명확 항목은 final만 쓰고 추천 문장을 버린다 — 공개 기준이 둘이 되지 않게.
      evidence(그 결정이 나온 원문 구절)는 둘 다 받는다.
    - source_text가 주어지면 근거를 원문과 대조한다. 명확 항목의 근거가 원문에 없으면
      final을 비워 돌려주고(호출자가 '확인 필요'로 올린다), 모호 항목은 근거만 비운다(하이라이트할
      자리가 없을 뿐 추천 문장은 그대로 쓸 수 있다).
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
        evidence = str(row.get("evidence", "")).strip() or (
            _legacy_candidate_evidence(row.get("candidates"), normalized_source) if ambiguous else ""
        )
        evidence_found = normalized_source is None or (
            bool(evidence) and services._normalize_for_match(evidence) in normalized_source
        )
        # 원문을 들고 검증하는 중이라면, 근거가 없는 확정 문장은 "원문에 있다"고 볼 수 없다.
        if final and not evidence_found:
            final = ""
        if ambiguous and not evidence_found:
            evidence = ""
        raw_candidates = row.get("candidate") if row.get("candidate") is not None else row.get("candidates")
        candidates, draft_hint = _parse_candidates(raw_candidates) if ambiguous else ([], "")
        items.append({
            "topic": topic,
            "ambiguous": ambiguous,
            "why": str(row.get("why", "")).strip() if ambiguous else "",
            "final": final,
            "evidence": evidence,
            "candidates": candidates,
            "draft_hint": draft_hint,
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
        evidences = {services._normalize_for_match(item["evidence"])} if item["evidence"] else set()
        prior = seen_evidence_by_topic.get(item["topic"])
        if evidences and prior and evidences & prior:
            continue
        seen_evidence_by_topic[item["topic"]] = (prior or set()) | evidences
        deduped.append(item)
    return deduped


# ---------------------------------------------------------------------------
# ①+② 한 번에 — 진행자가 "결정 사항 추출"을 눌렀을 때 들어오는 입구
# ---------------------------------------------------------------------------


def compile_meeting(meeting):
    """회의 녹취 원문을 추출해 Decision/Interpretation을 저장하고 요약을 돌려준다.

    {"decisions": n, "ambiguous": n, "few_candidates": n}
    - ambiguous: 진행자가 확인해야 하는 항목 수(AI가 갈린다고 봤거나 규칙이 걸린 것)
    - few_candidates: 확인이 필요한데 AI 추천 문장이 없는 항목 수. AI가 문장을 못 만들었거나
      모호어가 남아 버려진 경우다 — 진행자는 직접 쓰기나 '다음 회의로 넘김'으로만 처리할 수 있다.

    이미 Decision이 있는 회의는 건드리지 않는다(공개 뒤에 결정이 바뀌면 팀원이 확인한 내용과
    달라진다). 다시 추출하려면 호출 전에 draft 상태인 Decision을 지워야 한다.
    """
    if meeting.decisions.exists():
        raise DecisionCompileError("이미 결정 사항을 추출한 회의입니다. 다시 하려면 기존 결정을 먼저 지워 주세요.")
    source = meeting.transcript
    items = compile_specs(source)
    if not items:
        raise DecisionCompileError("녹취 원문에서 공개할 결정을 찾지 못했습니다.")

    ambiguous_count = 0
    few_candidates = 0
    try:
        ambiguous_count, few_candidates = _save_items(meeting, items)
    except IntegrityError:
        # 진행자가 추출 버튼을 두 번 눌렀을 때. 위의 exists() 검사는 LLM 호출(수 초) 앞에
        # 있으므로 두 요청이 모두 빈 상태를 보고 통과해 같은 order를 쓰려 한다. 먼저 저장한
        # 쪽의 결과를 그대로 두고, 늦게 온 쪽에는 사유를 보여준다(500 대신).
        raise DecisionCompileError("이미 결정 사항을 추출한 회의입니다. 화면을 새로 고쳐 주세요.")
    return {
        "decisions": len(items),
        "ambiguous": ambiguous_count,
        "few_candidates": few_candidates,
    }


def _looks_deferred(item):
    """결론을 다음으로 미루기로 한 근거 문장인가(DEFERRAL_PHRASES).

    그 결정이 나온 원문 구절을 본다 — 녹취 원문에서 가져온 텍스트라 AI가 쓴 topic/why보다 믿을 수
    있다.
    """
    return any(phrase in item["evidence"] for phrase in DEFERRAL_PHRASES)


def _save_items(meeting, items):
    """응답 항목들을 Decision/Interpretation으로 저장하고 (확인 필요 수, 후보 없는 수)를 돌려준다."""
    ambiguous_count = 0
    few_candidates = 0
    with transaction.atomic():
        for order, item in enumerate(items):
            warnings, divergence_reason, final_text, candidates, draft_hint = _resolve_item(item)
            # 미루기로 한 안건은 AI가 뭐라고 썼든 '다음 회의로 넘김'을 기본값으로 건다 —
            # 진행자가 수동으로 고르는 것과 똑같이 final_text/marked_undecided를 같이 채운다
            # (views._save_decision_edits의 CHOICE_UNDECIDED 처리와 동일).
            deferred = _looks_deferred(item)
            decision = Decision.objects.create(
                meeting=meeting, order=order, what=item["topic"],
                warnings=warnings, divergence_reason=divergence_reason,
                final_text=undecided_sentence(item["topic"]) if deferred else final_text,
                marked_undecided=deferred, draft_hint=draft_hint,
                # 그 결정이 나온 원문 구절 — 녹취 원문 창에서 하이라이트한다. AI 추천 문장에는
                # 근거를 따로 붙이지 않으므로 결정마다 이 하나뿐이다.
                evidence=item["evidence"],
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
    """응답 항목 하나 → (warnings, divergence_reason, final_text, candidates, draft_hint).

    세 갈래다.
    1. AI가 모호하다고 본 항목: AI의 이유와 후보를 그대로 쓴다. 규칙은 부르지 않는다 —
       이미 진행자에게 묻고 있으므로 중복 검사할 이유가 없다. draft_hint는 _parse_candidates가
       버린 후보에서 이미 만들어 둔 것을 그대로 쓴다.
    2. AI가 명확하다고 했지만 근거를 원문에서 확인하지 못한 항목(parse_items가 final을
       비운 경우): 확인 필요로 올리고 후보는 두지 않는다.
    3. AI가 명확하다고 하고 근거도 확인된 항목: 규칙 안전망을 돌린다. 걸리면 확인 필요로
       올리고 AI가 쓴 문장을 "이 문장 그대로 공개" 후보 하나로 보여준다. 걸리지 않으면 그
       문장이 곧 확정 내용이고 진행자는 탭하지 않는다. 경고가 모호어·지시어 때문이면 그
       문장에서 바로 draft_hint를 만든다 — "이 문장 그대로 공개" 후보를 고르지 않을 호스트를
       위한 출발점이다.
    """
    if item["ambiguous"]:
        # AI가 이유를 비워 보낼 수 있다. 사유가 비면 has_warning이 False가 되어 모호한 결정이
        # 확인 없이 공개되므로, 기본 문구로라도 채워 확인 대상으로 남긴다.
        return [], item["why"] or "AI가 다르게 이해할 수 있다고 봤어요", "", item["candidates"], item["draft_hint"]
    if not item["final"]:
        return [], "근거를 원문에서 확인하지 못했어요", "", [], ""
    warnings = safety_warnings(item["final"], item["evidence"])
    if not warnings:
        return [], "", item["final"], [], ""
    draft_hint = (
        _draft_hint_text(item["final"])
        if any(phrase in item["final"] for phrase in AMBIGUOUS_PHRASES + REFERENT_PHRASES) else ""
    )
    return (
        warnings, "", "",
        [{"text": item["final"], "evidence": "", "recommended": False}],
        draft_hint,
    )


# ---------------------------------------------------------------------------
# ③ 진행자 확정 + ④ 확정 문장 공개
#
# 진행자는 확인이 필요한 항목마다(Decision.needs_host) [AI 추천 문장 | 직접 쓰기 | 다음 회의로
# 넘김 | 이 결정 빼기] 중 하나를 고른다. 확인이 필요 없는 항목은 '직접 쓰기' 칸에 AI가 쓴 문장이
# 미리 들어 있고, 그 칸에 적힌 문장이 그대로 공개된다.
# 모든 항목을 고르지 않아도 공개할 수 있다 — 공개할 때 고르지 않은 확인 필요 항목은 '이 결정
# 빼기'로 처리된다(views._save_decision_edits). 공개할 문장이 없는 항목(Decision.blocks_publish)이
# 공개되는 일은 그래서 생기지 않고, publish_blockers는 마지막 그물로만 남는다.
# ---------------------------------------------------------------------------

# 진행자 확정 라디오 버튼 value. "candidate:12" 형태.
CHOICE_CANDIDATE_PREFIX = "candidate:"
CHOICE_CUSTOM = "custom"
CHOICE_UNDECIDED = "undecided"
# AI가 회의에 없던 결정을 만들어 냈을 때 그 항목을 지운다. '다음 회의로 넘김'과 달라야 하는
# 이유: 없던 안건을 이월하면 다음 회의마다 계속 보이게 된다.
CHOICE_DROP = "drop"


def context_around(source, evidence, window=1):
    """근거 문장이 전사본에서 실제로 있던 자리를 찾아 앞뒤로 window개씩 문장을 더해 돌려준다.

    근거 한 문장만으로는 판단하기 어려울 때(예: 확정 문장이 원문 여러 문장을 합쳐 썼는데
    근거는 그중 한 문장만 인용한 경우) 앞뒤 맥락을 보여주면 진행자가 판단하기 쉬워진다.
    근거를 원문에서 못 찾거나 앞뒤에 더할 문장이 없으면 빈 문자열을 돌려준다 — 화면에서
    근거와 똑같은 내용을 두 번 보여주지 않기 위해서다.

    경고에 찍힌 표현(예: '그쪽')이 왜 모호한지는 이 함수로 못 본다 — 그 표현은 근거 문장이
    아니라 확정 문장에서 나온 것이라 원문에서의 위치가 근거와 다를 수 있다. 그건 아래
    warning_context가 표현 자체를 원문에서 찾아 보여준다.
    """
    if not evidence or not source:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", source.strip())
    normalized_evidence = services._normalize_for_match(evidence)
    for i, sentence in enumerate(sentences):
        if services._normalize_for_match(sentence) == normalized_evidence:
            start, end = max(0, i - window), min(len(sentences), i + window + 1)
            if end - start <= 1:
                return ""
            return " ".join(sentences[start:end])
    return ""


_QUOTED_PHRASE = re.compile(r"'([^']+)'$")


def warning_context(source, warning, window=1):
    """경고 문구에 인용된 표현(예: "확정 문장에 모호한 표현이 있어요: '그쪽'")이 전사
    원문 어디서 나왔는지 앞뒤 맥락과 함께 보여준다.

    이 표현은 확정 문장(AI가 쓴 문장)에서 찾은 것이지, 근거 문장에서 찾은 게 아니다 — 그래서
    context_around와 달리 근거 위치가 아니라 '표현 자체'를 원문에서 직접 검색한다. 날짜·
    시각·숫자 충돌 경고("...섞여 있어요: 30개, 50개")처럼 인용 부호가 없는 경고는 그 자체로
    이미 어떤 값이 충돌하는지 다 보여주므로 빈 문자열을 돌려준다.
    """
    match = _QUOTED_PHRASE.search(warning)
    if not match or not source:
        return ""
    phrase = match.group(1)
    sentences = re.split(r"(?<=[.!?])\s+", source.strip())
    for i, sentence in enumerate(sentences):
        if phrase in sentence:
            start, end = max(0, i - window), min(len(sentences), i + window + 1)
            return " ".join(sentences[start:end])
    return ""


def undecided_sentence(topic):
    """'다음 회의로 넘김'을 고른 항목의 확정 문장.

    미결정 항목도 문장을 가져야 한다 — 공개되는 것은 어떤 상태에서나 final_text 하나이고,
    팀원 화면과 다음 회의의 이월 목록이 이 문장을 그대로 쓴다. 진행자는 탭 한 번만 하고
    문장은 서버가 만든다.
    """
    return "%s — 이번 회의에서 결론이 나지 않았음 (다음 회의로)" % topic


def publish_blockers(decisions):
    """공개할 문장이 없는 항목 — 확인이 필요한데 진행자가 아직 고르지 않은 것, 그리고 문장이 빈 것.

    화면은 이 수를 "고르지 않으면 빠지는 항목"으로 보여주고, 공개할 때 views가 이 항목들을
    '이 결정 빼기'로 처리한다. 그 뒤에도 남아 있으면 서버가 공개를 거부한다(마지막 그물).

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
    """공개된 항목의 확정 문장을 고치고 revised를 세운다 — 팀원 화면에서 "수정됨"으로 표시한다.

    팀원 알림은 호출한 쪽(views)이 보낸다. 예전 '확인했어요' 기록(DecisionAgreement)이 남아
    있으면 바뀐 문장과 맞지 않으므로 함께 지운다. 지워진 기록 수를 돌려준다.
    """
    with transaction.atomic():
        Decision.objects.filter(pk=decision.pk).update(final_text=text, revised=True)
        removed = decision.agreements.all().delete()[0]
    decision.final_text = text
    decision.revised = True
    return removed


def published_decisions(meeting):
    """팀원에게 보여줄 항목 — 공개된 것과 미결정으로 넘어간 것.

    draft는 절대 포함되지 않는다. 공개 전에는 팀원 화면에 확정된 결정이 아예 담기지 않아야
    하므로, 팀원 쪽 조회는 반드시 이 함수를 거친다.
    """
    return list(
        meeting.decisions
        .filter(status__in=[Decision.STATUS_PUBLISHED, Decision.STATUS_UNDECIDED])
        .prefetch_related("interpretations")
    )


# ---------------------------------------------------------------------------
# 녹취 원문 창 — 근거 문장의 위치
# ---------------------------------------------------------------------------


def _match_index(text):
    """대조용 문자열과 원래 위치 목록. 공백·문장부호를 지우고 남은 글자마다 원문 위치를 기억한다.

    근거 문장은 원문과 공백·부호가 조금씩 달라도 같은 문장으로 본다(_normalize_for_match와 같은
    기준을 부호까지 넓혔다) — STT 전사본은 쉼표·말줄임표가 들쭉날쭉해서다.
    """
    kept, positions = [], []
    for index, char in enumerate(text):
        if char.isalnum():
            kept.append(char)
            positions.append(index)
    return "".join(kept), positions


# 같은 근거 문장이 원문에 여러 번 나올 때, 각 자리 앞뒤로 이만큼(공백·부호를 뺀 글자 수)을 보고
# 결정 내용과 얼마나 겹치는지 잰다.
CONTEXT_WINDOW = 80


def _bigrams(text):
    return {text[i:i + 2] for i in range(len(text) - 1)}


def _occurrences(haystack, needle):
    """대조용 문자열에서 needle이 나오는 시작 위치 전부(겹쳐 나오는 것도 센다)."""
    found, at = [], haystack.find(needle)
    while at >= 0:
        found.append(at)
        at = haystack.find(needle, at + 1)
    return found


def _ranked_spans(source, evidence, context=""):
    """근거 문장이 원문에서 나오는 모든 (시작, 끝) 위치. 첫 번째가 대표 위치다.

    한 번만 나오면 그 자리다. 여러 번 나오면 각 자리 앞뒤 CONTEXT_WINDOW 글자가 context(그 결정의
    안건·확정 문장)와 두 글자 단위로 얼마나 겹치는지 보고 가장 많이 겹치는 자리를 대표로 앞에
    둔다 — "좋아요", "그렇게 하죠"처럼 짧은 맞장구는 회의 내내 되풀이되므로 처음 나온 자리를
    고르면 엉뚱한 대목을 가리키기 쉽다. 점수가 같으면 먼저 나온 자리를 대표로 둔다.
    """
    if not source or not evidence:
        return []
    haystack, positions = _match_index(source)
    needle, _ = _match_index(evidence)
    if not needle:
        return []
    starts = _occurrences(haystack, needle)
    if len(starts) > 1:
        wanted = _bigrams(_match_index(context)[0])
        def score(at):
            window = haystack[max(0, at - CONTEXT_WINDOW):at] + haystack[at + len(needle):at + len(needle) + CONTEXT_WINDOW]
            return len(_bigrams(window) & wanted)
        starts.sort(key=lambda at: -score(at))  # 정렬이 안정적이라 동점이면 먼저 나온 자리가 앞에 남는다.
    return [(positions[at], positions[at + len(needle) - 1] + 1) for at in starts]


def evidence_span(source, evidence, context=""):
    """근거 문장의 대표 위치 (시작, 끝). 못 찾으면 None.

    끝 위치는 마지막 글자 바로 뒤라서 source[start:end]가 그 구간이다. 여러 번 나오면
    context(그 결정의 안건·확정 문장)와 가장 가까운 자리를 고른다(_ranked_spans).
    """
    spans = _ranked_spans(source, evidence, context)
    return spans[0] if spans else None


def evidence_spans(source, evidences, context=""):
    """[{"start", "end", "label", "primary"}, ...] — 화면에서 하이라이트할 구간. 원문에서 찾은 것만, 겹치지 않게.

    evidences는 [(label, 근거 문장), ...]이다. label은 화면에서 어느 후보의 근거인지 표시하는 데
    쓴다(예: "후보 A"). 같은 구간이 여러 후보의 근거면 한 번만 담고 꼬리표를 합친다.

    근거 문장이 원문에 여러 번 나오면 모든 자리를 담되, context와 가장 가까운 대표 자리만
    primary=True다. 화면은 대표 자리로 스크롤하고 나머지는 옅게 칠한다 — 고른 자리가 틀렸어도
    사람이 다른 후보 자리를 볼 수 있게 하려는 것이다.
    """
    spans = {}
    for label, evidence in evidences:
        for rank, span in enumerate(_ranked_spans(source, evidence, context)):
            entry = spans.setdefault(span, {"labels": [], "primary": False})
            if label and label not in entry["labels"]:
                entry["labels"].append(label)
            entry["primary"] = entry["primary"] or rank == 0
    merged = []
    # 같은 자리에서 시작하면 긴 구간(문장 전체)을 먼저 둔다.
    for (start, end), entry in sorted(spans.items(), key=lambda item: (item[0][0], -item[0][1])):
        span = {"start": start, "end": end, "label": " · ".join(entry["labels"]), "primary": entry["primary"]}
        # 서로 걸쳐 있는 구간은 하나만 남긴다 — 하이라이트가 겹치면 화면에서 경계가 사라진다.
        # 대표 자리가 아닌 쪽이 대표 자리를 가리지 않도록, 앞의 것이 대표가 아니고 뒤의 것이 대표면 바꾼다.
        if merged and start < merged[-1]["end"]:
            if span["primary"] and not merged[-1]["primary"]:
                merged[-1] = span
            continue
        merged.append(span)
    return merged


# ---------------------------------------------------------------------------
# ④ 미결정 이월
# ---------------------------------------------------------------------------


# 미결정 사유를 하나도 찾지 못했을 때 보여줄 문구.
CARRYOVER_DEFAULT_REASON = "이번 회의에서 결론이 나지 않았어요"


def carryover_reason(decision):
    """이월 안건의 미결정 사유 한 줄 — AI가 본 '왜 갈리나', 없으면 첫 경고, 둘 다 없으면 기본 문구."""
    if decision.divergence_reason:
        return decision.divergence_reason
    if decision.warnings:
        return decision.warnings[0]
    return CARRYOVER_DEFAULT_REASON


def _participant_names(meeting):
    """그 회의의 참석자 이름 — 진행자를 맨 앞에, 나머지는 이름순.

    참석자를 고르지 않은 회의(기능 도입 전에 만든 회의 포함)는 알림 범위와 같은 규칙으로 팀 멤버
    전원을 참석자로 본다(notifications._recipients). prefetch된 목록만 써서 회의마다 쿼리가 늘지 않게 한다.
    """
    people = list(meeting.participants.all()) or list(meeting.team.members.all())
    people.sort(key=lambda user: (user.pk != meeting.host_id, user.username))
    return [user.username for user in people]


def carryover_decisions(meeting):
    """이 회의보다 먼저 만들어진 같은 팀 회의의 미결정 안건 중 아직 '처리됨'이 안 된 것.

    진행자가 '처리됨'을 누를 때까지(carryover_resolved_at이 채워질 때까지) 이후 회의마다
    계속 보인다 — 한 번 보고 사라지면 묻히기 때문이다.

    화면에 쓰는 임시 속성을 붙여 돌려준다(저장되지 않는다).
    - participant_names: 그 안건이 나온 회의의 참석자 이름 목록(상단 "회의명 (참석자)")
    - undecided_reason: 미결정 사유 한 줄(하단 "안건 – 미결정 사유")
    """
    decisions = list(
        Decision.objects
        .filter(
            meeting__team_id=meeting.team_id,
            meeting__created_at__lt=meeting.created_at,
            status=Decision.STATUS_UNDECIDED,
            carryover_resolved_at__isnull=True,
        )
        .select_related("meeting", "meeting__team")
        .prefetch_related("meeting__participants", "meeting__team__members")
        .order_by("meeting__created_at", "order", "id")
    )
    for decision in decisions:
        decision.participant_names = _participant_names(decision.meeting)
        decision.undecided_reason = carryover_reason(decision)
    return decisions
