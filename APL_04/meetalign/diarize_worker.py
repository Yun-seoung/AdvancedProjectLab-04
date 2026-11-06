"""로컬 화자 분리 워커. services.transcribe()가 별도 프로세스로 띄워서 실행한다(whisper_worker.py와 같은 이유 — 모델이 서버
프로세스 메모리에 계속 남지 않게). Django를 import하지 않는 독립 스크립트다.

    python diarize_worker.py <녹음 파일> <모델 폴더> [화자 수(0=자동)] [threshold]

sherpa-onnx(pip install sherpa-onnx)로 pyannote 분할 모델 + NeMo TitaNet 화자 임베딩을 돌린다. pyannote 원본과 달리 Hugging Face
토큰이 필요 없고 CPU에서 돈다. 모델(약 47MB)이 모델 폴더에 없으면 sherpa-onnx GitHub 릴리스에서 받아 둔다.
결과는 [[시작초, 끝초, 화자번호], ...] JSON 한 줄로 stdout에 출력한다. 실패하면 0이 아닌 코드로 끝나고 stderr에 이유가 남는다."""

import json
import os
import sys
import tarfile
import urllib.request

RATE = 16000
_RELEASES = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
SEGMENTATION_ARCHIVE = _RELEASES + "/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
SEGMENTATION_MODEL = os.path.join("sherpa-onnx-pyannote-segmentation-3-0", "model.onnx")
# 한국어 합성 음성 4명 테스트에서 화자 수를 주지 않아도 4명을 맞게 나눈 조합이다(3D-Speaker ERes2Net보다 2배 빠름).
EMBEDDING_URL = _RELEASES + "/speaker-recongition-models/nemo_en_titanet_small.onnx"
EMBEDDING_MODEL = "nemo_en_titanet_small.onnx"
DEFAULT_THRESHOLD = 0.8


def ensure_models(model_dir):
    os.makedirs(model_dir, exist_ok=True)
    segmentation = os.path.join(model_dir, SEGMENTATION_MODEL)
    if not os.path.exists(segmentation):
        archive = os.path.join(model_dir, "segmentation.tar.bz2")
        urllib.request.urlretrieve(SEGMENTATION_ARCHIVE, archive)
        with tarfile.open(archive) as tar:
            tar.extractall(model_dir, filter="data")
        os.remove(archive)
    embedding = os.path.join(model_dir, EMBEDDING_MODEL)
    if not os.path.exists(embedding):
        urllib.request.urlretrieve(EMBEDDING_URL, embedding + ".part")
        os.replace(embedding + ".part", embedding)
    return segmentation, embedding


def load_audio(path):
    """16kHz 모노 float32. PyAV(faster-whisper와 함께 설치됨)가 있으면 어떤 형식이든, 없으면 WAV만 읽는다."""
    import numpy as np
    try:
        import av
    except ImportError:
        import wave
        with wave.open(path, "rb") as wav_file:
            if wav_file.getframerate() != RATE or wav_file.getnchannels() != 1 or wav_file.getsampwidth() != 2:
                raise RuntimeError("PyAV가 없으면 16kHz 모노 16bit WAV만 화자 분리할 수 있습니다 (pip install av)")
            return np.frombuffer(wav_file.readframes(wav_file.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    resampler = av.AudioResampler(format="flt", layout="mono", rate=RATE)
    chunks = []
    with av.open(path) as container:
        for frame in container.decode(audio=0):
            chunks.extend(out.to_ndarray().reshape(-1) for out in resampler.resample(frame))
        chunks.extend(out.to_ndarray().reshape(-1) for out in resampler.resample(None))
    return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)


def diarize(path, model_dir, num_speakers=0, threshold=DEFAULT_THRESHOLD):
    import sherpa_onnx

    segmentation, embedding = ensure_models(model_dir)
    threads = max(1, min(4, os.cpu_count() or 1))
    config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=segmentation), num_threads=threads,
        ),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=embedding, num_threads=threads),
        # 화자 수를 알면 그만큼으로 묶고, 모르면(0) threshold로 자동 판단한다. 값이 클수록 화자를 적게 나눈다.
        clustering=sherpa_onnx.FastClusteringConfig(num_clusters=num_speakers or -1, threshold=threshold),
        min_duration_on=0.3, min_duration_off=0.5,
    )
    result = sherpa_onnx.OfflineSpeakerDiarization(config).process(load_audio(path)).sort_by_start_time()
    return [[round(turn.start, 2), round(turn.end, 2), turn.speaker] for turn in result]


def main(argv):
    path, model_dir = argv[1:3]
    num_speakers = int(argv[3]) if len(argv) > 3 and argv[3] else 0
    threshold = float(argv[4]) if len(argv) > 4 and argv[4] else DEFAULT_THRESHOLD
    print(json.dumps(diarize(path, model_dir, num_speakers, threshold)))


if __name__ == "__main__":
    main(sys.argv)
