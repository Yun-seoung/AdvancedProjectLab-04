"""LLM/STT 연결 지점. OLLAMA_MODEL이나 MLX_WHISPER_MODEL 환경변수가 있을 때만 로컬 모델을 쓰고, 없으면 현재 동작을 그대로 쓴다."""

import json
import os
import re
import urllib.request

FAKE_CHECK_QUESTIONS = [
    "이번 회의의 핵심 결정 사항은?",
    "내가 맡은 다음 할 일은?",
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


def _generate(prompt):
    num_ctx = int(os.environ.get("OLLAMA_NUM_CTX", "16384"))
    body = json.dumps({
        "model": os.environ["OLLAMA_MODEL"],
        "prompt": prompt,
        "stream": False,
        "options": {"num_ctx": num_ctx},
    }).encode("utf-8")
    request = urllib.request.Request(OLLAMA_URL, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))["response"].strip()


def _generate_clean(prompt, retries=1):
    """_generate()를 호출하고 한국어/영어 외 언어가 섞이면 같은 프롬프트로 재시도한다.
    연결 실패는 OSError를 그대로 올리고(호출자가 구분해서 처리), 재시도 후에도 언어가
    깨끗하지 않으면 None을 반환한다 — 연결은 됐지만 출력 품질이 나쁜 경우를 구분하기 위함."""
    result = None
    for attempt in range(retries + 1):
        result = _generate(prompt)
        if _is_korean_or_english(result):
            return result
    return None


def _question_count_for(record):
    """회의록이 다룬 항목이 많을수록 이해도 확인 질문도 늘린다 (최소 2개, 최대 5개).
    [안건]/[결정 사항]/[할 일] 아래 '-' 불릿 개수를 복잡도 지표로 쓴다."""
    bullets = sum(1 for line in record.splitlines() if line.strip().startswith("-"))
    return max(2, min(5, bullets // 2))


def check_questions(meeting):
    if not (_enabled() and meeting.record):
        return list(FAKE_CHECK_QUESTIONS)
    count = _question_count_for(meeting.record)
    prompt = (
        "다음 회의록을 읽고, 팀원들이 서로 다르게 이해했을 수 있는 핵심 질문을 한 줄에 하나씩 정확히 %d개 써라. "
        "반드시 한국어로만 쓰고 다른 언어를 섞지 마라. 번호나 설명 없이 질문만 써라.\n\n회의록:\n%s"
        % (count, meeting.record)
    )
    try:
        raw = _generate_clean(prompt)
    except OSError:
        return list(FAKE_CHECK_QUESTIONS)
    if raw is None:
        return list(FAKE_CHECK_QUESTIONS)
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    return lines[:count] or list(FAKE_CHECK_QUESTIONS)


def summarize(transcript):
    if not (_enabled() and transcript):
        return transcript
    prompt = (
        "다음은 회의 녹음의 전사본이다. 한국어로만, 아래 형식으로 정리하라. 전사본에 없는 내용은 쓰지 마라.\n"
        "[안건]\n- ...\n[결정 사항]\n- ...\n[할 일]\n- ...\n\n전사본:\n" + transcript
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


def is_consistent(texts, record="", question=""):
    if len({t.strip() for t in texts}) <= 1:
        return True
    if not (_enabled() and record and question):
        return False
    prompt = (
        "회의록과 질문, 그리고 팀원들의 답변이 주어진다. 답변들이 회의록에 비추어 같은 내용을 가리키면 '일치', "
        "서로 다르게 이해한 부분이 있으면 '불일치'라고만 답하라. 반드시 한국어로만 답하라.\n\n"
        "회의록:\n" + record + "\n\n질문: " + question + "\n\n답변:\n" + "\n".join("- " + t for t in texts)
    )
    try:
        verdict = _generate_clean(prompt)
    except OSError:
        return False
    if verdict is None:
        return False
    return "불일치" not in verdict


def explain_discrepancy(record, question, answers):
    """팀원 답변이 엇갈릴 때, 무엇이 왜 다르게 이해됐는지 한두 문장으로 설명한다.
    로컬 LLM이 꺼져 있거나 실패하면 빈 문자열을 반환해 화면에서 설명 없이도 답변 목록만 보이게 한다."""
    if not (_enabled() and record and question and answers):
        return ""
    prompt = (
        "회의록과 질문, 팀원들의 서로 다른 답변이 주어진다. 답변들이 구체적으로 어떤 부분에서 다르게 이해됐는지 "
        "한두 문장으로 짧게 설명하라. 반드시 한국어로만 답하고, 설명 외에 다른 말은 하지 마라.\n\n"
        "회의록:\n" + record + "\n\n질문: " + question + "\n\n답변:\n" + "\n".join("- " + t for t in answers)
    )
    try:
        result = _generate_clean(prompt)
    except OSError:
        return ""
    return result or ""
