"""음성 전사(STT) 격리 계층.

공급자는 환경변수 STT_PROVIDER 로 고른다 (mock | gemini).
뷰는 transcribe() 만 호출하고 공급자 사정은 알지 못한다.
"""

import os

#: mock 공급자가 돌려주는 가상 회의 전사. 실제 회의 녹음이 아니다.
SAMPLE_TRANSCRIPT = """오늘 회의는 스터디앱 1차 배포 범위를 정하기 위한 자리입니다.
지난주에 나온 기능 다섯 개 중에서 어디까지 넣을지가 쟁점입니다.
로그인, 스터디 생성, 출석 체크 세 가지는 1차 배포에 포함하기로 결정했습니다.
알림 기능은 1차에서 빼고 2차로 넘기는 것으로 확정했습니다.
통계 화면은 아직 논의 중이고 다음 회의에서 다시 이야기합니다.
1차 배포일은 11월 14일 금요일로 잡았습니다.
코드 프리즈는 배포 3일 전인 11월 11일입니다.
서버 비용은 현재 견적으로 월 4만 원 정도이고 상한은 월 6만 원으로 두기로 했습니다.
출석 체크 API는 이도현 씨가 맡고 로그인 화면은 박서연 씨가 맡습니다.
스터디 생성 화면은 진행자가 직접 맡기로 했습니다.
우리가 말하는 출석률은 참석한 모임 수를 개설된 모임 수로 나눈 값으로 정의합니다.
1차에서는 수동 테스트만 하고 자동화 테스트는 범위에서 제외합니다.
베타 사용자는 20명으로 제한합니다.
정리하면 세 기능, 11월 14일 배포, 코드 프리즈 11월 11일입니다.
통계 화면만 미결로 남겨 둡니다."""


class SttError(Exception):
    """전사에 실패했을 때. 뷰는 이 메시지를 화면에 보여준다."""


def provider_name():
    return (os.environ.get("STT_PROVIDER") or "mock").strip().lower()


def transcribe(audio_path):
    """오디오 파일 경로를 받아 전사 텍스트를 돌려준다."""
    provider = provider_name()
    if provider == "mock":
        return _mock_transcribe(audio_path)
    raise SttError("지원하지 않는 STT_PROVIDER 입니다: %s" % provider)


def _mock_transcribe(audio_path):
    """파일 내용을 읽지 않고 가상 회의 전사를 돌려준다 (키 없이 시연하기 위한 공급자)."""
    if not os.path.exists(audio_path):
        raise SttError("업로드된 파일을 찾을 수 없습니다.")
    return SAMPLE_TRANSCRIPT
