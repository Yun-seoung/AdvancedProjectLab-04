"""로컬 Whisper 전사 워커. services.transcribe()가 별도 프로세스로 띄워서 실행한다(이유는 services.py의 _transcribe_local 참고).
Django를 import하지 않는 독립 스크립트라 어떤 파이썬으로도 실행할 수 있다.

    python whisper_worker.py mlx    <녹음 파일> <모델>                        # Apple Silicon Mac 전용 (mlx-whisper)
    python whisper_worker.py faster <녹음 파일> <모델> [device] [compute_type]  # Windows/Linux/Mac 공통 (faster-whisper)

전사한 텍스트를 JSON 문자열 하나로 stdout에 출력한다. 맨 끝에 --segments를 붙이면 화자 분리용으로 단어 단위 목록
[{"start","end","text"}, ...]을 출력한다. 실패하면 0이 아닌 코드로 끝나고 stderr에 이유가 남는다."""

import json
import os
import sys
import sysconfig


def _add_windows_cuda_dlls():
    """Windows에서 pip로 설치한 NVIDIA 라이브러리(nvidia-cublas-cu12, nvidia-cudnn-cu12)의 DLL 폴더를 찾아 검색 경로에 더한다.
    faster-whisper(CTranslate2)가 GPU를 쓰려면 cuBLAS와 cuDNN DLL이 PATH에 있어야 하는데, pip 패키지는 site-packages/nvidia/*/bin에
    풀어 둘 뿐 PATH에 넣어 주지 않아서 'Could not locate cudnn_ops64_9.dll' 같은 오류로 바로 죽는다. 설치돼 있지 않으면 아무것도 안 한다."""
    if sys.platform != "win32":
        return
    base = os.path.join(sysconfig.get_paths()["purelib"], "nvidia")
    if not os.path.isdir(base):
        return
    for name in sorted(os.listdir(base)):
        bin_dir = os.path.join(base, name, "bin")
        if os.path.isdir(bin_dir):
            os.add_dll_directory(bin_dir)
            os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")


def transcribe_mlx(path, model, with_segments=False):
    import mlx_whisper
    if with_segments:
        result = mlx_whisper.transcribe(path, path_or_hf_repo=model, language="ko", word_timestamps=True)
        return [
            {"start": w["start"], "end": w["end"], "text": w["word"].strip()}
            for s in result["segments"] for w in s.get("words", []) if w["word"].strip()
        ]
    return mlx_whisper.transcribe(path, path_or_hf_repo=model, language="ko")["text"]


def transcribe_faster(path, model, device="auto", compute_type="default", with_segments=False):
    """device는 auto(기본, CUDA가 되면 GPU 아니면 CPU)/cpu/cuda, compute_type은 default/int8/float16 등(CPU에서는 int8이 빠르다).
    vad_filter로 말이 없는 구간을 건너뛰어 긴 회의 녹음에서 시간을 아끼고 묵음 구간의 환각(없는 말을 지어내는 것)을 줄인다."""
    _add_windows_cuda_dlls()
    from faster_whisper import WhisperModel

    whisper = WhisperModel(model, device=device, compute_type=compute_type)
    if with_segments:
        # 화자 분리용: 문장 구간은 화자 경계를 넘어 이어지는 일이 잦아서 단어 단위 시각(문장부호 포함)을 쓴다.
        segments, _info = whisper.transcribe(path, language="ko", vad_filter=True, word_timestamps=True)
        return [
            {"start": w.start, "end": w.end, "text": w.word.strip()}
            for s in segments for w in (s.words or []) if w.word.strip()
        ]
    segments, _info = whisper.transcribe(path, language="ko", vad_filter=True)
    return " ".join(text for text in (segment.text.strip() for segment in segments) if text)


def main(argv):
    # 맨 끝의 --segments는 화자 분리용: 텍스트 대신 [{"start","end","text"}, ...]를 출력한다.
    options = {}
    if argv[-1] == "--segments":
        argv, options = argv[:-1], {"with_segments": True}
    engine, path, model = argv[1:4]
    if engine == "mlx":
        text = transcribe_mlx(path, model, **options)
    else:
        text = transcribe_faster(path, model, *argv[4:6], **options)
    print(json.dumps(text))


if __name__ == "__main__":
    main(sys.argv)
