"""음성 전사(STT) 격리 계층.

공급자는 환경변수 STT_PROVIDER 로 고른다 (mock | gemini).
뷰는 transcribe() 만 호출하고 공급자 사정은 알지 못한다.
"""

import os

#: mock 공급자가 돌려주는 가상 회의 전사. 실제 회의 녹음이 아니다.
#: 붙여넣기 시연용으로 같은 내용이 samples/demo_meeting.txt 에도 있다.
SAMPLE_TRANSCRIPT = """1차 배포에 어디까지 넣을지 정해야 합니다.
로그인과 스터디 생성, 출석 체크까지 넣기로 하죠.
알림 기능은 2차로 넘기는 것으로 확정합니다.
통계 화면은 아직 논의 중입니다.
1차 배포일은 11월 14일로 잡겠습니다.
코드 프리즈는 11월 11일입니다.
출석 체크 API는 이도현 씨가 맡습니다.
로그인 화면은 박서연 씨가 맡습니다.
출석률은 참석 모임 수를 개설 모임 수로 나눈 값으로 정의합니다.
베타 사용자는 20명으로 제한합니다.
자동화 테스트는 1차 범위에서 제외합니다."""


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
