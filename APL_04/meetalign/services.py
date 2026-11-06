"""LLM/STT 연결 지점. LLM은 Gemini(GEMINI_API_KEY+GEMINI_MODEL) → Groq(GROQ_API_KEY+GROQ_LLM_MODEL) →
로컬 Ollama(OLLAMA_MODEL) 순으로 설정된 것을 쓰고, 앞 단계가 없거나 호출이 실패하면 다음으로 넘어간다.
GROQ_API_KEY는 STT(Whisper)와 LLM 양쪽에 같은 키를 쓴다 — Groq 콘솔에서 발급 한 번이면 된다.
STT는 CLOVA_SPEECH_INVOKE_URL과 CLOVA_SPEECH_SECRET_KEY가 모두 있으면 네이버 CLOVA Speech를 쓰되, 이번 달 무료 사용량
(CLOVA_SPEECH_FREE_MINUTES, 기본 20분)을 다 썼거나 호출이 실패하면 GROQ_API_KEY가 있을 때 Groq의 Whisper API로,
그마저 없거나 실패하면 로컬 Whisper(MLX_WHISPER_MODEL 또는 FASTER_WHISPER_MODEL)로 넘어간다. 해당 환경변수가 없으면
현재(가짜) 동작을 그대로 쓴다."""

import json
import logging
import math
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
import wave

from django.db.models import Sum
from django.utils import timezone

from .models import SttUsage

logger = logging.getLogger(__name__)

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434/api/generate")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent"
# 무료 등급은 응답 대기가 길어서 Ollama보다 넉넉하게 기다린다 (GEMINI_TIMEOUT으로 조정).
GEMINI_DEFAULT_TIMEOUT_SECONDS = 300
# 바로 실패하는 일시적 오류(요청 과다, 서버 바쁨)만 잠시 쉬었다가 다시 보낸다. 타임아웃은 재시도하지 않는다.
GEMINI_RETRY_STATUS = (429, 500, 503)
GEMINI_RETRIES = 3
GEMINI_RETRY_DELAY_SECONDS = 5
TIMEOUT_SECONDS = 120
# 한국어/영어 외 문자(일본어 가나, 중국어 한자 등)가 섞여나오는 LLM 출력을 걸러낸다.
_FOREIGN_SCRIPT = re.compile(r"[぀-ヿ一-鿿]")


def _gemini_enabled():
    return bool(os.environ.get("GEMINI_API_KEY") and os.environ.get("GEMINI_MODEL"))


GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"


def _groq_llm_enabled():
    return bool(os.environ.get("GROQ_API_KEY") and os.environ.get("GROQ_LLM_MODEL"))


def _enabled():
    return _gemini_enabled() or _groq_llm_enabled() or bool(os.environ.get("OLLAMA_MODEL"))


def _is_korean_or_english(text):
    return not _FOREIGN_SCRIPT.search(text)


TRANSCRIBE_TIMEOUT_SECONDS = 1800
_WHISPER_WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "whisper_worker.py")
LOCAL_STT_LABELS = {"mlx": "로컬 모델(mlx-whisper)", "faster": "로컬 모델(faster-whisper)"}
# CLOVA Speech Free 플랜은 매월 20분을 무료로 준다. 네이버 클라우드는 15초 단위로 올림해서 과금하므로 사용량도 같은 단위로 기록한다.
CLOVA_FREE_MINUTES_DEFAULT = 20
CLOVA_BILLING_UNIT_SECONDS = 15


def _clova_enabled():
    return bool(os.environ.get("CLOVA_SPEECH_INVOKE_URL") and os.environ.get("CLOVA_SPEECH_SECRET_KEY"))


GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_DEFAULT_MODEL = "whisper-large-v3-turbo"


def _groq_enabled():
    return bool(os.environ.get("GROQ_API_KEY"))


def _local_stt_engine():
    """설정된 로컬 Whisper 엔진. mlx-whisper(Apple Silicon 전용)가 있으면 그것을, 아니면 faster-whisper(Windows 포함 모든 OS)."""
    if os.environ.get("MLX_WHISPER_MODEL"):
        return "mlx"
    if os.environ.get("FASTER_WHISPER_MODEL"):
        return "faster"
    return None


def _clova_limit_seconds():
    return int(os.environ.get("CLOVA_SPEECH_FREE_MINUTES", CLOVA_FREE_MINUTES_DEFAULT)) * 60


def clova_used_seconds():
    """이번 달(1일 0시부터) CLOVA Speech로 전사한 음성 길이 합계(초)."""
    month_start = timezone.localtime().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return SttUsage.objects.filter(created_at__gte=month_start).aggregate(total=Sum("audio_seconds"))["total"] or 0


def _round_up_billing(seconds):
    return max(math.ceil(seconds / CLOVA_BILLING_UNIT_SECONDS), 1) * CLOVA_BILLING_UNIT_SECONDS


def _audio_duration_seconds(path):
    """녹음 길이(초). faster-whisper와 함께 설치되는 PyAV가 있으면 어떤 형식이든 읽고, 없으면 WAV만 표준 라이브러리로 읽는다.
    둘 다 안 되면(브라우저 녹음 webm은 길이 정보가 없는 경우도 있다) None — 길이를 모르는 채로 진행한다."""
    try:
        import av
        with av.open(path) as container:
            if container.duration:
                return container.duration / 1_000_000
    except Exception:
        pass
    try:
        with wave.open(path, "rb") as wav_file:
            return wav_file.getnframes() / wav_file.getframerate()
    except Exception:
        return None


def _clova_budget_allows(duration):
    """이번 달 남은 무료 사용량 안에서 이 녹음을 CLOVA로 보낼 수 있는지. 길이를 모르면 한도가 남아 있기만 하면 보낸다
    (호출 뒤 응답으로 사용량을 기록하므로, 그 한 건만큼은 한도를 넘길 수 있다)."""
    used, limit = clova_used_seconds(), _clova_limit_seconds()
    if duration is None:
        return used < limit
    return used + _round_up_billing(duration) <= limit


def _clova_multipart(path, params):
    """urllib에는 multipart 인코더가 없어서 직접 만든다. 녹음 파일(최대 500MB)을 통째로 메모리에 올리지 않도록 본문을
    머리말 → 파일 조각들 → 꼬리말 순서로 내보내는 제너레이터와, 미리 계산한 Content-Length, Content-Type을 돌려준다."""
    boundary = uuid.uuid4().hex
    head = (
        '--%s\r\nContent-Disposition: form-data; name="params"\r\nContent-Type: application/json\r\n\r\n%s\r\n'
        '--%s\r\nContent-Disposition: form-data; name="media"; filename="%s"\r\nContent-Type: application/octet-stream\r\n\r\n'
        % (boundary, json.dumps(params), boundary, os.path.basename(path))
    ).encode("utf-8")
    tail = ("\r\n--%s--\r\n" % boundary).encode("utf-8")

    def body():
        yield head
        with open(path, "rb") as audio:
            while chunk := audio.read(1024 * 1024):
                yield chunk
        yield tail

    return body(), len(head) + os.path.getsize(path) + len(tail), "multipart/form-data; boundary=%s" % boundary


def _clova_text(data, segments, diarize):
    """화자 구분을 켰고 화자가 둘 이상 나오면 발언을 "화자 A: ..." 줄로 묶어서 돌려주고, 아니면 전체 텍스트를 그대로 돌려준다."""
    if diarize:
        turns = []
        for segment in segments:
            speaker = segment.get("speaker") or {}
            name, text = speaker.get("name") or speaker.get("label") or "?", (segment.get("text") or "").strip()
            if not text:
                continue
            if turns and turns[-1][0] == name:
                turns[-1][1] += " " + text
            else:
                turns.append([name, text])
        if len({name for name, _ in turns}) > 1:
            return "\n".join("화자 %s: %s" % (name, text) for name, text in turns)
    return (data.get("text") or " ".join((segment.get("text") or "").strip() for segment in segments)).strip()


def _transcribe_clova(path):
    """CLOVA Speech 장문 인식(로컬 파일 업로드, 동기 방식) 호출. 전사 텍스트와, 응답의 마지막 구간 끝 시각으로 추정한 음성
    길이(초)를 돌려준다. 네트워크/HTTP 오류, 해석 불가 응답, COMPLETED가 아닌 결과는 모두 이유를 담은 RuntimeError로 올린다."""
    diarize = os.environ.get("CLOVA_SPEECH_DIARIZATION") == "1"
    params = {"language": "ko-KR", "completion": "sync", "wordAlignment": False, "fullText": True, "diarization": {"enable": diarize}}
    body, length, content_type = _clova_multipart(path, params)
    request = urllib.request.Request(
        os.environ["CLOVA_SPEECH_INVOKE_URL"].rstrip("/") + "/recognizer/upload", data=body, method="POST",
        headers={
            "Content-Type": content_type, "Content-Length": str(length),
            "X-CLOVASPEECH-API-KEY": os.environ["CLOVA_SPEECH_SECRET_KEY"],
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TRANSCRIBE_TIMEOUT_SECONDS) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace").strip()[:200] if error.fp is not None else ""
        raise RuntimeError("CLOVA Speech 오류 %d: %s" % (error.code, detail or error.reason)) from error
    except OSError as error:
        raise RuntimeError("CLOVA Speech 연결 실패: %s" % error) from error
    except ValueError as error:
        raise RuntimeError("CLOVA Speech 응답 해석 실패: %s" % error) from error
    if data.get("result") != "COMPLETED":
        raise RuntimeError("CLOVA Speech 전사 실패: %s" % (data.get("message") or data.get("result") or str(data)[:200]))
    segments = data.get("segments") or []
    end_seconds = max((segment.get("end") or 0 for segment in segments), default=0) / 1000
    return _clova_text(data, segments, diarize), end_seconds


def _groq_multipart(path, model):
    """urllib에는 multipart 인코더가 없어서 직접 만든다. 녹음 파일(최대 500MB)을 통째로 메모리에 올리지 않도록 본문을
    머리말 → 파일 조각들 → 꼬리말 순서로 내보내는 제너레이터와, 미리 계산한 Content-Length, Content-Type을 돌려준다."""
    boundary = uuid.uuid4().hex
    head = (
        '--%s\r\nContent-Disposition: form-data; name="model"\r\n\r\n%s\r\n'
        '--%s\r\nContent-Disposition: form-data; name="language"\r\n\r\nko\r\n'
        '--%s\r\nContent-Disposition: form-data; name="response_format"\r\n\r\ntext\r\n'
        '--%s\r\nContent-Disposition: form-data; name="file"; filename="%s"\r\nContent-Type: application/octet-stream\r\n\r\n'
        % (boundary, model, boundary, boundary, boundary, os.path.basename(path))
    ).encode("utf-8")
    tail = ("\r\n--%s--\r\n" % boundary).encode("utf-8")

    def body():
        yield head
        with open(path, "rb") as audio:
            while chunk := audio.read(1024 * 1024):
                yield chunk
        yield tail

    return body(), len(head) + os.path.getsize(path) + len(tail), "multipart/form-data; boundary=%s" % boundary


def _transcribe_groq(path):
    """Groq의 OpenAI 호환 Whisper 엔드포인트 호출(로컬 파일 업로드). response_format=text로 받아 전사 텍스트를
    그대로 돌려준다(JSON 파싱 불필요). CLOVA와 달리 무료 사용량을 앱에서 추적하지 않는다(Groq 콘솔에서 확인).
    HTTP/네트워크 오류는 이유를 담은 RuntimeError로 올린다."""
    model = os.environ.get("GROQ_MODEL", GROQ_DEFAULT_MODEL)
    body, length, content_type = _groq_multipart(path, model)
    request = urllib.request.Request(
        GROQ_URL, data=body, method="POST",
        headers={
            "Content-Type": content_type, "Content-Length": str(length),
            "Authorization": "Bearer %s" % os.environ["GROQ_API_KEY"],
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TRANSCRIBE_TIMEOUT_SECONDS) as response:
            return response.read().decode("utf-8").strip()
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace").strip()[:200] if error.fp is not None else ""
        raise RuntimeError("Groq Whisper 오류 %d: %s" % (error.code, detail or error.reason)) from error
    except OSError as error:
        raise RuntimeError("Groq Whisper 연결 실패: %s" % error) from error


def _transcribe_local(recording_path, engine):
    """로컬 Whisper(mlx-whisper 또는 faster-whisper)는 별도 프로세스(whisper_worker.py)로 띄워서 실행한다. Django 개발 서버는
    요청마다 새 스레드를 쓰는데, mlx의 GPU 스트림이 스레드에 묶여 있어서 같은 프로세스 안에서 다른 스레드가 연달아 호출하면
    간헐적으로 'no Stream(gpu, 1) in current thread' 에러가 난다. 매번 새 프로세스에서 실행하면 항상 그 프로세스의 메인
    스레드에서 처음 쓰는 상태가 되어 이 문제를 피할 수 있다. faster-whisper도 같은 방식으로 돌려서 수 GB짜리 모델이 서버
    프로세스 메모리에 계속 남지 않게 한다."""
    if engine is None:
        return None
    if engine == "mlx":
        args = ["mlx", recording_path, os.environ["MLX_WHISPER_MODEL"]]
    else:
        args = [
            "faster", recording_path, os.environ["FASTER_WHISPER_MODEL"],
            os.environ.get("FASTER_WHISPER_DEVICE", "auto"), os.environ.get("FASTER_WHISPER_COMPUTE_TYPE", "default"),
        ]
    result = subprocess.run(
        [sys.executable, _WHISPER_WORKER] + args, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=TRANSCRIBE_TIMEOUT_SECONDS, env={**os.environ, "PYTHONUTF8": "1"},
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or "로컬 Whisper 전사 실패").strip()[-500:])
    return json.loads(result.stdout.strip()).strip()


def transcribe(recording_path, recording_id=None):
    """녹음 파일을 전사한다. 네이버 CLOVA Speech(이번 달 무료 사용량 이내) → Groq Whisper API → 로컬 Whisper 순서로
    시도하고, 앞 단계가 설정돼 있지 않거나 실패하면 다음 단계로 넘어간다. 아무 것도 설정돼 있지 않으면 None(가짜 동작
    유지). recording_id는 CLOVA 사용량 기록에 어떤 녹음이었는지 남기는 용도."""
    local = _local_stt_engine()
    fallback_available = _groq_enabled() or local is not None

    if _clova_enabled():
        duration = _audio_duration_seconds(recording_path)
        if _clova_budget_allows(duration):
            try:
                text, end_seconds = _transcribe_clova(recording_path)
            except RuntimeError as error:
                if not fallback_available:
                    raise
                logger.warning("%s — 다음 전사 엔진으로 전환합니다", error)
            else:
                SttUsage.objects.create(recording_id=recording_id, audio_seconds=_round_up_billing(max(duration or 0, end_seconds)))
                return text
        else:
            message = "CLOVA Speech 이번 달 무료 사용량(%d분)을 다 썼습니다" % (_clova_limit_seconds() // 60)
            if not fallback_available:
                raise RuntimeError(message + ". Groq나 로컬 Whisper를 설정하면 그쪽으로 넘어갑니다")
            logger.info("%s — 다음 전사 엔진으로 전사합니다", message)

    if _groq_enabled():
        try:
            return _transcribe_groq(recording_path)
        except RuntimeError as error:
            if local is None:
                raise
            logger.warning("%s — %s로 전환합니다", error, LOCAL_STT_LABELS[local])

    return _transcribe_local(recording_path, local)


def _format_minutes(seconds):
    return ("%.1f" % (seconds / 60)).rstrip("0").rstrip(".")


def _fallback_stt_label():
    """CLOVA 다음으로 넘어갈 전사 엔진 이름(화면 안내용). Groq가 설정돼 있으면 그걸, 아니면 로컬 엔진. 둘 다 없으면 None."""
    if _groq_enabled():
        return "Groq Whisper"
    return LOCAL_STT_LABELS.get(_local_stt_engine())


def stt_status_text():
    """회의 상세 화면의 녹음 업로드 폼에 보여줄 전사 엔진 안내 한 줄. 어떤 STT도 설정돼 있지 않으면 None."""
    fallback = _fallback_stt_label()
    if not _clova_enabled():
        return "전사 엔진: %s" % fallback if fallback else None
    used, limit = clova_used_seconds(), _clova_limit_seconds()
    status = "전사 엔진: CLOVA Speech · 이번 달 무료 사용량 %s/%d분" % (_format_minutes(used), limit // 60)
    if used >= limit:
        return status + (" (소진 → %s로 전사)" % fallback if fallback else " (소진 — 로컬 모델이 없어 전사가 실패합니다)")
    return status + (" (소진 시 %s로 전환)" % fallback if fallback else "")


def _generate(prompt, temperature=None):
    if _gemini_enabled():
        try:
            return _generate_gemini(prompt, temperature=temperature)
        except OSError:
            # 재시도 후에도 Gemini가 안 되면(과부하, 타임아웃, 키 오류 등) Groq나 Ollama가
            # 설정돼 있을 때만 그쪽으로 넘어간다.
            if not (_groq_llm_enabled() or os.environ.get("OLLAMA_MODEL")):
                raise
    if _groq_llm_enabled():
        try:
            return _generate_groq_llm(prompt, temperature=temperature)
        except OSError:
            if not os.environ.get("OLLAMA_MODEL"):
                raise
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


def _generate_gemini(prompt, temperature=None):
    """Gemini generateContent API 호출. HTTP/네트워크 오류(401 잘못된 키, 429 무료 한도 초과 등)는
    urllib이 OSError 하위 예외로 올리므로 호출자의 기존 대체 처리가 그대로 동작한다. 안전 필터 차단 등으로
    응답에 텍스트가 없을 때도 같은 경로로 처리되도록 OSError로 바꿔서 올린다.
    429/500/503은 GEMINI_RETRIES번까지 점점 길게 쉬었다가 다시 보내고, 타임아웃은 다시 보내도 처음부터
    다시 기다려야 해서 재시도 없이 바로 올린다."""
    model = os.environ["GEMINI_MODEL"]
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    if temperature is not None:
        payload["generationConfig"] = {"temperature": temperature}
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        GEMINI_URL % model, data=body,
        headers={"Content-Type": "application/json", "x-goog-api-key": os.environ["GEMINI_API_KEY"]},
    )
    timeout = int(os.environ.get("GEMINI_TIMEOUT", GEMINI_DEFAULT_TIMEOUT_SECONDS))
    for attempt in range(GEMINI_RETRIES + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as error:
            if error.code not in GEMINI_RETRY_STATUS or attempt == GEMINI_RETRIES:
                raise
            time.sleep(GEMINI_RETRY_DELAY_SECONDS * (attempt + 1))
    try:
        parts = data["candidates"][0]["content"]["parts"]
        text = "".join(part.get("text", "") for part in parts if not part.get("thought"))
    except (KeyError, IndexError, TypeError):
        raise OSError("Gemini 응답에 텍스트가 없습니다: %s" % str(data)[:300])
    if not text.strip():
        raise OSError("Gemini 응답에 텍스트가 없습니다")
    return text.strip()


def _generate_groq_llm(prompt, temperature=None):
    """Groq의 OpenAI 호환 채팅 API 호출. HTTPError/URLError는 모두 OSError의 하위 클래스라 별도
    처리 없이도 그대로 올라가 호출자의 기존 대체 처리(다음 단계로 넘어가기)가 동작한다."""
    payload = {"model": os.environ["GROQ_LLM_MODEL"], "messages": [{"role": "user", "content": prompt}]}
    if temperature is not None:
        payload["temperature"] = temperature
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        GROQ_CHAT_URL, data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer %s" % os.environ["GROQ_API_KEY"]},
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        data = json.loads(response.read().decode("utf-8"))
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise OSError("Groq 응답에 텍스트가 없습니다: %s" % str(data)[:300])
    if not text.strip():
        raise OSError("Groq 응답에 텍스트가 없습니다")
    return text.strip()


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


def _normalize_for_match(text):
    return re.sub(r"\s+", "", text)


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
        return "(LLM 연결 실패: Gemini API 키나 Ollama 실행 상태를 확인하세요)"
    if result is None:
        return "(LLM 응답 언어 오류: 다시 시도해 주세요)"
    return result


