"""LLM/STT 연결 지점. OLLAMA_MODEL이나 MLX_WHISPER_MODEL 환경변수가 있을 때만 로컬 모델을 쓰고, 없으면 현재 동작을 그대로 쓴다."""

import json
import os
import re
import urllib.request

FAKE_CHECK_QUESTIONS = [
    {"text": "이번 회의의 핵심 결정 사항은?", "choices": ["A안으로 진행한다", "B안으로 진행한다", "아직 결정되지 않았다", "다음 회의로 미룬다"], "correct_choice": 0, "evidence": ""},
    {"text": "내가 맡은 다음 할 일은?", "choices": ["문서 작성", "코드 리뷰", "디자인 시안 제작", "회의 일정 조율"], "correct_choice": 0, "evidence": ""},
]
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434/api/generate")
TIMEOUT_SECONDS = 120
# 한국어/영어 외 문자(일본어 가나, 중국어 한자 등)가 섞여나오는 LLM 출력을 걸러낸다.
_FOREIGN_SCRIPT = re.compile(r"[぀-ヿ一-鿿]")


def _enabled():
    return bool(os.environ.get("OLLAMA_MODEL"))


def _is_korean_or_english(text):
    return not _FOREIGN_SCRIPT.search(text)


def transcribe(recording_path):
    model = os.environ.get("MLX_WHISPER_MODEL")
    if not model:
        return None
    import mlx_whisper
    return mlx_whisper.transcribe(recording_path, path_or_hf_repo=model, language="ko")["text"].strip()


def _generate(prompt, temperature=None):
    num_ctx = int(os.environ.get("OLLAMA_NUM_CTX", "16384"))
    options = {"num_ctx": num_ctx}
    if temperature is not None:
        options["temperature"] = temperature
    body = json.dumps({
        "model": os.environ["OLLAMA_MODEL"],
        "prompt": prompt,
        "stream": False,
        "options": options,
    }).encode("utf-8")
    request = urllib.request.Request(OLLAMA_URL, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))["response"].strip()


def _generate_clean(prompt, retries=1, temperature=None):
    """_generate()를 호출하고 한국어/영어 외 언어가 섞이면 같은 프롬프트로 재시도한다.
    연결 실패는 OSError를 그대로 올리고(호출자가 구분해서 처리), 재시도 후에도 언어가
    깨끗하지 않으면 None을 반환한다 — 연결은 됐지만 출력 품질이 나쁜 경우를 구분하기 위함."""
    result = None
    for attempt in range(retries + 1):
        result = _generate(prompt, temperature=temperature)
        if _is_korean_or_english(result):
            return result
    return None


def _parse_mcq_json(raw):
    """LLM이 낸 JSON 배열을 파싱한다. 코드블록 표시나 설명이 섞여 나와도 JSON 배열
    부분만 추출을 시도한다. 형식이 안 맞는 문항은 버리고, 하나도 없으면 빈 리스트를 반환한다."""
    match = re.search(r"\[.*\]", raw, re.DOTALL)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    questions = []
    for item in data:
        try:
            text = str(item["question"]).strip()
            choices = [str(c).strip() for c in item["choices"]]
            correct_choice = int(item["answer_index"])
            evidence = str(item.get("evidence", "")).strip()
        except (KeyError, TypeError, ValueError):
            continue
        if not text or len(choices) != 4 or any(not c for c in choices) or not (0 <= correct_choice < 4):
            continue
        questions.append({"text": text, "choices": choices, "correct_choice": correct_choice, "evidence": evidence})
    return questions


def generate_check_questions(meeting):
    """회의록에서 사람마다 다르게 해석할 수 있는 모호한 표현(애매한 기한, 불분명한 범위,
    담당자 불명확 등)을 찾아, 그 부분을 올바르게 이해했는지 확인하는 객관식 문제를 만든다.
    문항 개수는 고정이 아니라 실제로 찾은 모호한 표현 개수만큼(최대 5개)."""
    if not (_enabled() and meeting.record):
        return list(FAKE_CHECK_QUESTIONS)
    prompt = (
        "다음은 회의록이다. 이 회의록에서 사람마다 다르게 해석할 수 있는 모호한 표현을 최대 5개 찾아라 "
        "(예: 애매한 기한, 범위가 불분명한 지시, 담당자가 명확하지 않은 할 일, 조건부로만 설명된 내용 등). "
        "각 모호한 표현마다 그 부분을 올바르게 이해했는지 확인하는 객관식 문제를 하나씩 만들어라. "
        "보기는 반드시 4개이고, 그중 하나만 회의록 내용과 정확히 일치하는 정답이며, 나머지 3개는 "
        "그럴듯하지만 틀린, 흔히 오해하기 쉬운 해석으로 만들어라. evidence에는 정답의 근거가 되는 "
        "회의록 원문 문장을 그대로 써라.\n\n"
        "반드시 아래 JSON 배열 형식으로만 답하라. 설명이나 코드블록 표시 없이 JSON만 출력하라:\n"
        '[{"question": "질문", "choices": ["보기1", "보기2", "보기3", "보기4"], '
        '"answer_index": 0, "evidence": "근거 문장"}]\n\n'
        "회의록:\n" + meeting.record
    )
    try:
        raw = _generate_clean(prompt, temperature=0.3)
    except OSError:
        return list(FAKE_CHECK_QUESTIONS)
    if raw is None:
        return list(FAKE_CHECK_QUESTIONS)
    questions = _parse_mcq_json(raw)
    return questions[:5] if questions else list(FAKE_CHECK_QUESTIONS)


def summarize(transcript):
    if not (_enabled() and transcript):
        return transcript
    prompt = (
        "다음은 회의 녹음의 전사본이다. 한국어로만, 아래 형식으로 정리하라. 전사본에 없는 내용은 쓰지 말고, "
        "해당하는 내용이 전혀 없는 섹션은 통째로 생략하라.\n\n"
        "[요약]\n- 회의 전체 내용을 2~4줄로 요약\n\n"
        "[결정된 사항]\n- 회의에서 확정된 것만\n\n"
        "[아직 논의 중인 사항]\n- 결론 안 나고 보류되거나 이견이 남은 것\n\n"
        "[할 일]\n- 담당자가 언급됐으면 \"할 일 (담당자)\" 형식으로, 안 됐으면 할 일만\n\n"
        "[숫자와 기한]\n- 날짜, 마감일, 수치 등 구체적인 숫자가 나온 항목만 (조건부로 설명된 기한이면 그 조건까지 그대로)\n\n"
        "[모호한 표현]\n- 사람마다 다르게 해석할 수 있는 표현이 있다면 \"표현 — 왜 모호한지 한 줄\" 형식으로. "
        "예: 범위가 불분명한 지시, 애매한 기한, 담당자가 안 정해진 할 일 등\n\n"
        "전사본:\n" + transcript
    )
    try:
        result = _generate_clean(prompt)
    except OSError:
        return transcript
    return result if result is not None else transcript


def chat_reply(meeting, text, history=None):
    if not (_enabled() and meeting.record):
        return "(가짜 LLM 응답) '%s'에 대한 답변입니다." % text
    history_block = ""
    if history:
        lines = ["%s: %s" % ("나" if role == "user" else "AI", msg) for role, msg in history]
        history_block = "\n\n이전 대화 (참고해서 자연스럽게 이어서 답하라):\n" + "\n".join(lines)
    prompt = (
        "아래 회의록 내용만 근거로 질문에 답하라. 반드시 한국어로만 답하고 다른 언어를 섞지 마라. 회의록에 없는 내용이면 '회의록에 없는 내용입니다'라고 답하라.\n\n"
        "회의록:\n" + meeting.record + history_block + "\n\n질문: " + text
    )
    try:
        result = _generate_clean(prompt)
    except OSError:
        return "(LLM 연결 실패: Ollama가 실행 중인지 확인하세요)"
    if result is None:
        return "(LLM 응답 언어 오류: 다시 시도해 주세요)"
    return result


