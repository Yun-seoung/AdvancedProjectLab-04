"""결정 컴파일러(실험 기능)의 ① 컴파일과 ② 해석 후보 생성.

① 컴파일: 회의 전사 원문에서 결정을 뽑아 실행 명세
   [누가 · 무엇을 · 언제까지 · 완료 기준 · 확정 여부]로 옮기고, 빈칸·모호어·숫자 충돌이
   있으면 '컴파일 경고'를 붙인다. 경고는 회의당 최대 MAX_WARNED_DECISIONS개까지만 남긴다.
② 해석 후보: 경고가 붙은 항목마다 AI가 '가상 청자'가 되어 실행 결과가 서로 달라지는 해석
   2~3개를 만든다. 각 해석의 근거 문장이 전사 원문에 실제로 없으면 그 해석은 버린다
   (기존 이해도 퀴즈의 evidence 검증과 같은 방식). 정답은 만들지 않는다.
   이 후보는 진행자에게만 참고로 보여준다 — 진행자가 고르거나 직접 쓴 한 줄이 확정 내용이 된다.

※ 블라인드 투표 모드(보류): 팀원 전원이 후보를 블라인드로 동시 선택하고 집계로 판정하는
   방식을 먼저 만들었다가(DecisionVote + tallies_for/judge/finalize) 진행자 확정 모드로
   방향을 바꿨다. 되살릴 때 참고할 구현은 커밋 7eb074a(블라인드 선택 화면)과
   ac6fb89(전원일치/갈림/미결정 판정)에 있다. 아래 ①② 코드는 그때와 같으므로, 집계·판정
   함수와 DecisionVote 모델만 다시 붙이면 된다.

※ services.py의 내부(_로 시작하는) 함수에 의존한다. 아래 셋의 이름이나 동작이 바뀌면 이
   모듈도 같이 고쳐야 한다(퀴즈 삭제 때도 이 셋은 그래서 남겼다).
   - services._enabled()
       Gemini(GEMINI_API_KEY+GEMINI_MODEL) 또는 Ollama(OLLAMA_MODEL)가 설정돼 있는지.
       False면 LLM을 아예 부르지 않고 가짜 응답(FAKE_DECISIONS 등)으로 전체 흐름을 시연한다.
   - services._generate_clean(prompt, retries=1, temperature=None)
       프롬프트를 Gemini → Ollama 순으로 보내고 응답 문자열을 돌려준다. 한국어/영어 외
       언어가 섞이면 재시도 후 None을 주고, 연결 자체가 실패하면 OSError를 올린다.
   - services._normalize_for_match(text)
       공백을 모두 지운 비교용 문자열. 근거 문장이 전사 원문에 있는지 볼 때 쓴다.
"""

import json
import re

from django.db import transaction

from . import services
from .models import Decision, DecisionAgreement, Interpretation

# 경고는 회의당 최대 5개. 이보다 많이 발견되면 앞쪽(전사 순서) 5개만 경고로 남기고
# 나머지는 명세로만 보여준다 — 한 회의에서 다룰 수 있는 안건 수를 넘기지 않기 위함.
MAX_WARNED_DECISIONS = 5
# 명세 자체는 경고 유무와 무관하게 이만큼까지 받는다(LLM이 회의록을 잘게 쪼개는 경우 방지).
MAX_DECISIONS = 12
MAX_INTERPRETATIONS = 3
# 후보가 1개 이하로 남으면 "서로 다른 해석"을 보여줄 수 없어 진행자가 고를 것이 없다.
# 그 항목은 직접 작성이나 '아직 미정'으로만 처리할 수 있다.
MIN_INTERPRETATIONS = 2
# 파싱 실패 시 같은 프롬프트로 한 번 더 보내고, 그래도 안 되면 실패 처리한다.
PARSE_RETRIES = 1

# 명세에 섞이면 사람마다 다르게 읽는 표현. 팀에서 합의한 목록이라 자동 추출이 아니라 고정 목록이다.
AMBIGUOUS_PHRASES = ("쯤", "시간 되면", "각자", "나중에", "아까 정한 대로")

CONFIRM_STATE_FROM_TEXT = {
    "확정": Decision.CONFIRM_CONFIRMED,
    "논의중": Decision.CONFIRM_DISCUSSING,
    "논의 중": Decision.CONFIRM_DISCUSSING,
    "미정": Decision.CONFIRM_UNSET,
}

# 날짜 표현 하나를 통째로 잡는다. "12월 5일"을 "12월"과 "5일" 두 개로 쪼개면 날짜가 하나뿐인
# 명세에도 충돌 경고가 붙으므로, 긴 형태를 먼저 매칭하도록 순서를 고정했다.
_DATE_EXPR = re.compile(
    r"\d{1,2}\s*월\s*\d{1,2}\s*일"
    r"|\d{1,2}\s*월"
    r"|\d{1,2}\s*일"
    r"|[월화수목금토일]요일"
    r"|\d+\s*주\s*(?:뒤|후|내)?"
    r"|\d{1,2}\s*시(?:\s*\d{1,2}\s*분)?"
)
_QUANTITY_EXPR = re.compile(r"\d+\s*(?:개|명|건|차|퍼센트|%)")

FAKE_DECISIONS = [
    {"who": "", "what": "로그인 화면 개선안 적용", "due": "다음 주쯤", "done": "", "state": "논의중"},
    {"who": "민수", "what": "배포 스크립트 정리", "due": "금요일", "done": "스테이징에서 한 번 돌려보기", "state": "확정"},
    {"who": "각자", "what": "담당 화면 QA", "due": "시간 되면", "done": "", "state": "미정"},
]


class DecisionCompileError(Exception):
    """LLM 응답을 재시도까지 하고도 명세/해석으로 못 읽었을 때. 진행자 화면에 사유를 그대로 띄운다."""


# ---------------------------------------------------------------------------
# 경고 판정 — LLM이 아니라 이쪽에서 규칙으로 센다. 같은 전사본이면 항상 같은 경고가 나와야
# 하고(재현성), "경고 최대 5개" 같은 제한도 여기서 지켜야 하기 때문이다.
# ---------------------------------------------------------------------------


def _blank_warnings(spec):
    warnings = []
    if not spec["who"]:
        warnings.append("담당자 빈칸")
    if not spec["due"]:
        warnings.append("기한 빈칸")
    if not spec["done"]:
        warnings.append("완료 기준 빈칸")
    return warnings


def _ambiguous_warnings(spec):
    joined = " ".join((spec["who"], spec["what"], spec["due"], spec["done"]))
    return ["모호어 '%s'" % phrase for phrase in AMBIGUOUS_PHRASES if phrase in joined]


def _date_tokens(text):
    return {re.sub(r"\s+", "", m.group(0)) for m in _DATE_EXPR.finditer(text)}


def _quantity_tokens(text):
    return {re.sub(r"\s+", "", m.group(0)) for m in _QUANTITY_EXPR.finditer(text)}


def _conflict_warnings(spec):
    """한 명세 안에 날짜(또는 수량)가 둘 이상 섞여 있으면 경고. 예: "금요일 아니면 다음 주 월요일"."""
    text = " ".join((spec["what"], spec["due"], spec["done"]))
    warnings = []
    dates = _date_tokens(text)
    if len(dates) >= 2:
        warnings.append("같은 항목에 다른 날짜 %s" % ", ".join(sorted(dates)))
    quantities = _quantity_tokens(text)
    if len(quantities) >= 2:
        warnings.append("같은 항목에 다른 숫자 %s" % ", ".join(sorted(quantities)))
    return warnings


def _cross_warnings(specs):
    """같은 담당자에게 서로 다른 기한이 걸려 있으면 양쪽 모두에 경고를 붙인다.

    {명세 index: [경고, ...]} 를 돌려준다. 담당자가 빈칸인 명세는 "같은 대상"으로 묶을 수
    없으므로 제외한다(그건 이미 '담당자 빈칸' 경고가 잡는다).
    """
    by_owner = {}
    for index, spec in enumerate(specs):
        if spec["who"]:
            by_owner.setdefault(spec["who"], []).append(index)
    extra = {}
    for owner, indexes in by_owner.items():
        if len(indexes) < 2:
            continue
        tokens = {}
        for index in indexes:
            found = _date_tokens(specs[index]["due"])
            if found:
                tokens[index] = found
        distinct = set().union(*tokens.values()) if tokens else set()
        if len(tokens) >= 2 and len(distinct) >= 2:
            for index in tokens:
                extra.setdefault(index, []).append(
                    "'%s'에게 다른 날짜 %s" % (owner, ", ".join(sorted(distinct)))
                )
    return extra


def warnings_for(specs):
    """[명세, ...] → [경고 목록, ...]. 경고가 붙은 명세는 최대 MAX_WARNED_DECISIONS개까지만 남는다.

    잘라낼 때는 뒤쪽(전사본에서 나중에 나온) 명세의 경고를 비운다 — 회의 앞부분에서 정한
    것일수록 뒤 안건의 전제가 되므로 먼저 맞춰야 한다.
    """
    cross = _cross_warnings(specs)
    rows = []
    for index, spec in enumerate(specs):
        found = _blank_warnings(spec) + _ambiguous_warnings(spec) + _conflict_warnings(spec)
        found += cross.get(index, [])
        # 같은 사유가 두 번 들어가지 않게 하면서 순서는 유지한다.
        seen = []
        for warning in found:
            if warning not in seen:
                seen.append(warning)
        rows.append(seen)
    kept = 0
    for index, found in enumerate(rows):
        if not found:
            continue
        if kept < MAX_WARNED_DECISIONS:
            kept += 1
        else:
            rows[index] = []
    return rows


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


def parse_decisions(raw):
    """LLM 응답 → [{"who","what","due","done","state"}, ...].

    '무엇을'이 비어 있으면 실행 명세가 될 수 없으므로 그 항목은 버린다. 반대로 담당자·기한·
    완료 기준의 빈칸은 버리지 않고 그대로 둔다 — 그 빈칸이 바로 컴파일 경고의 재료다.
    """
    data = _json_array(raw)
    if data is None:
        return []
    specs = []
    for item in data:
        if not isinstance(item, dict):
            continue
        what = str(item.get("what", "")).strip()
        if not what:
            continue
        state = str(item.get("state", "")).strip()
        specs.append({
            "who": str(item.get("who", "")).strip(),
            "what": what,
            "due": str(item.get("due", "")).strip(),
            "done": str(item.get("done", "")).strip(),
            "state": CONFIRM_STATE_FROM_TEXT.get(state, Decision.CONFIRM_UNSET),
        })
        if len(specs) >= MAX_DECISIONS:
            break
    return specs


def _compile_prompt(source):
    return (
        "다음은 회의 녹음의 전사 원문이다. 이 회의에서 나온 '결정'을 찾아, 각각을 실행 명세로 "
        "옮겨라. 실행 명세는 [누가 · 무엇을 · 언제까지 · 완료 기준 · 확정 여부]다.\n"
        "반드시 전사본에 실제로 나온 내용만 쓰고, 빈칸을 추측으로 채우지 마라. 전사본에서 "
        "담당자나 기한이 언급되지 않았으면 그 칸은 빈 문자열로 남겨라 — 빈칸 자체가 중요한 "
        "정보다. 서로 다른 발언을 임의로 연결하지 마라.\n"
        "state는 \"확정\", \"논의중\", \"미정\" 중 하나로만 쓴다. 결론이 난 것은 확정, 이견이 "
        "남은 것은 논의중, 말만 나오고 아무것도 정해지지 않은 것은 미정이다.\n\n"
        "반드시 아래 JSON 배열 형식으로만 답하라. 설명이나 코드블록 표시 없이 JSON만 출력하라:\n"
        '[{"who": "담당자", "what": "할 일", "due": "기한", "done": "완료 기준", "state": "확정"}]\n\n'
        "전사본:\n" + source
    )


def _generate_parsed(prompt, parser, what_failed, soft_fail=False):
    """프롬프트를 보내고 파싱까지 성공할 때까지 최대 (1 + PARSE_RETRIES)번 시도한다.

    - 연결 실패(OSError)와 응답 언어 오류는 None을 돌려준다 → 호출자가 가짜 응답으로 대체.
    - 연결은 됐는데 형식이 재시도 후에도 안 맞으면:
      soft_fail=False(① 컴파일)는 DecisionCompileError를 올린다 — 명세가 없으면 화면에
        보여줄 게 아무것도 없으므로 회의 단위로 실패시키고 진행자에게 사유를 띄운다.
      soft_fail=True(② 해석 분기)는 빈 리스트를 돌려준다 — 해석 후보가 0개인 것은 항목
        하나가 투표에 못 오를 뿐이지, 나머지 항목의 명세까지 버릴 이유는 아니다.
        근거 검증에서 해석이 전부 걸러진 경우도 같은 경로로 들어온다.
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
    if soft_fail:
        return []
    raise DecisionCompileError(
        "AI 응답을 %s로 읽지 못했습니다. 잠시 뒤 다시 시도해 주세요." % what_failed
    )


def compile_specs(source):
    """전사 원문 → [명세, ...]. LLM이 없거나 연결이 안 되면 가짜 명세를 돌려준다."""
    if not (services._enabled() and source):
        return parse_decisions(json.dumps(FAKE_DECISIONS))
    specs = _generate_parsed(_compile_prompt(source), parse_decisions, "실행 명세")
    if specs is None:
        return parse_decisions(json.dumps(FAKE_DECISIONS))
    return specs


# ---------------------------------------------------------------------------
# ② 해석 분기
# ---------------------------------------------------------------------------


def parse_interpretations(raw, source_text=None):
    """LLM 응답 → [{"text","evidence"}, ...].

    source_text가 주어지면 evidence가 그 원문에 실제로 등장하는지 확인하고, 지어낸 근거가
    붙은 해석은 버린다. evidence가 빈 문자열인 해석은 검증을 건너뛴다 — 가짜 응답 모드에서
    근거 없이 흐름만 시연할 때 쓰는 통로다.
    """
    data = _json_array(raw)
    if data is None:
        return []
    normalized_source = services._normalize_for_match(source_text) if source_text is not None else None
    results = []
    for item in data:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        evidence = str(item.get("evidence", "")).strip()
        if not text:
            continue
        if normalized_source is not None and evidence and services._normalize_for_match(evidence) not in normalized_source:
            continue
        if any(text == kept["text"] for kept in results):
            continue
        results.append({"text": text, "evidence": evidence})
        if len(results) >= MAX_INTERPRETATIONS:
            break
    return results


def _interpretation_prompt(spec_line, warnings, source):
    return (
        "너는 이 회의에 참석한 '가상 청자'다. 아래 실행 명세 한 줄은 사람마다 다르게 읽힐 수 "
        "있어서 경고가 붙었다. 이 명세를 읽은 팀원들이 실제로 서로 다른 결과를 만들게 되는 "
        "해석을 2~3개 쓰라.\n"
        "해석은 말만 다른 게 아니라 실행 결과가 달라져야 한다(예: 기한이 이번 주 금요일 vs 다음 "
        "주 금요일, 범위가 화면 하나 vs 전체 흐름). 어느 해석이 맞는지는 쓰지 마라 — 정답은 "
        "없다.\n"
        "evidence에는 그 해석의 근거가 된 전사본 원문 문장을 토씨 하나 틀리지 않고 그대로 복사해서 "
        "써라. 원문에 없는 문장을 쓰면 그 해석은 버려진다.\n\n"
        "실행 명세: %s\n"
        "붙은 경고: %s\n\n"
        "반드시 아래 JSON 배열 형식으로만 답하라. 설명이나 코드블록 표시 없이 JSON만 출력하라:\n"
        '[{"text": "해석 내용", "evidence": "근거가 된 원문 문장"}]\n\n'
        "전사본:\n%s" % (spec_line, ", ".join(warnings) or "없음", source)
    )


def _fake_interpretations(decision):
    """가짜 응답 모드용. 명세의 빈칸·모호어가 어떻게 갈리는지 보여주는 고정 해석 2개.

    evidence를 비워 두어 근거 검증을 건너뛴다(원문이 없는 시연 상황이기 때문).
    """
    who = decision.who or "담당자 미정"
    due = decision.due_text or "기한 미정"
    return [
        {"text": "%s이(가) 혼자 맡아 %s 안에 끝낸다는 뜻" % (who, due), "evidence": ""},
        {"text": "%s이(가) 초안만 잡고 완료 시점은 다음 회의에서 다시 정한다는 뜻" % who, "evidence": ""},
    ]


def interpretations_for(decision, source):
    """경고가 붙은 Decision 하나에 대한 해석 후보. 근거 검증을 통과한 것만 돌려준다."""
    if not (services._enabled() and source):
        return _fake_interpretations(decision)
    prompt = _interpretation_prompt(decision.spec_line, decision.warnings, source)
    results = _generate_parsed(
        prompt, lambda raw: parse_interpretations(raw, source_text=source), "해석 후보",
        soft_fail=True,
    )
    if results is None:
        return _fake_interpretations(decision)
    return results


# ---------------------------------------------------------------------------
# ①+② 한 번에 — 진행자가 "결정 컴파일 실행"을 눌렀을 때 들어오는 입구
# ---------------------------------------------------------------------------


def compile_meeting(meeting):
    """회의 전사 원문을 컴파일해 Decision/Interpretation을 저장하고 요약을 돌려준다.

    {"decisions": n, "warned": n, "dropped_warnings": n, "few_candidates": n}
    - warned: 경고가 붙어 해석 후보 생성까지 간 항목 수
    - dropped_warnings: 경고가 났지만 회의당 5개 제한에 걸려 경고를 비운 항목 수
    - few_candidates: 근거 검증을 통과한 후보가 MIN_INTERPRETATIONS개 미만인 항목 수
      (진행자가 고를 후보가 부족해 직접 작성이나 '아직 미정'으로만 처리할 수 있는 항목)

    이미 Decision이 있는 회의는 건드리지 않는다(공개 뒤에 명세가 바뀌면 팀원이 확인한 내용과
    달라진다). 다시 컴파일하려면 호출 전에 draft 상태인 Decision을 지워야 한다.
    """
    if meeting.decisions.exists():
        raise DecisionCompileError("이미 컴파일된 회의입니다. 다시 하려면 기존 명세를 먼저 지워 주세요.")
    source = meeting.transcript
    specs = compile_specs(source)
    if not specs:
        raise DecisionCompileError("전사 원문에서 실행 명세로 옮길 결정을 찾지 못했습니다.")
    all_warnings = warnings_for(specs)
    # 제한에 걸려 경고가 비워진 항목 수. 사유가 있었는지는 비우기 전 판정으로 다시 센다.
    dropped = sum(
        1 for index, spec in enumerate(specs)
        if not all_warnings[index]
        and (_blank_warnings(spec) or _ambiguous_warnings(spec) or _conflict_warnings(spec))
    )

    warned = 0
    few_candidates = 0
    with transaction.atomic():
        for order, (spec, warnings) in enumerate(zip(specs, all_warnings)):
            decision = Decision.objects.create(
                meeting=meeting, order=order,
                who=spec["who"], what=spec["what"], due_text=spec["due"],
                done_criteria=spec["done"], confirm_state=spec["state"], warnings=warnings,
            )
            if not warnings:
                continue
            warned += 1
            candidates = interpretations_for(decision, source)
            for interpretation_order, candidate in enumerate(candidates):
                Interpretation.objects.create(
                    decision=decision, order=interpretation_order,
                    text=candidate["text"], evidence=candidate["evidence"],
                )
            if len(candidates) < MIN_INTERPRETATIONS:
                few_candidates += 1
    return {
        "decisions": len(specs), "warned": warned,
        "dropped_warnings": dropped, "few_candidates": few_candidates,
    }


# ---------------------------------------------------------------------------
# ③ 진행자 확정 + ④ 확정 명세 공개
#
# 진행자는 경고가 붙은 항목마다 [후보 선택 | 직접 작성 | 아직 미정] 중 하나를 고른다.
# 경고가 없는 항목은 명세 자체가 확정 내용이라 고를 것이 없고(Decision.is_settled), 공개
# 대상에는 똑같이 포함된다 — 팀원은 그 항목도 확인한다.
# ---------------------------------------------------------------------------

# 진행자 확정 라디오 버튼 value. "candidate:12" 형태.
CHOICE_CANDIDATE_PREFIX = "candidate:"
CHOICE_CUSTOM = "custom"
CHOICE_UNDECIDED = "undecided"


def publish_blockers(decisions):
    """공개를 막는 항목 — 경고가 붙었는데 진행자가 아직 아무것도 고르지 않은 것.

    이 목록이 비어 있어야 "확정 명세 공개"를 누를 수 있다. 화면의 버튼 비활성화와 서버의
    거부가 같은 판정을 쓰도록 여기 한 곳에서만 센다.
    """
    return [decision for decision in decisions if not decision.is_settled]


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
