# MeetAlign (합의착시탐지기)

회의 녹음을 올리면 회의록을 정리하고, 팀원들이 서로 다르게 이해한 부분을 짚어주는 서비스입니다.

- 녹음 업로드 → 로컬 STT(mlx-whisper)로 전사 → 로컬 LLM(Ollama)으로 회의록 요약
- 회의록 기반 이해도 확인 질문 자동 생성, 팀원별 답변 수집
- 답변이 엇갈리면 의미 비교로 불일치를 탐지하고, 무엇이 왜 다르게 이해됐는지 설명
- 익명 질문, 팀 초대 코드, 다크 모드, 마이크 직접 녹음 등 지원

## 데이터 프라이버시

회의 녹음 전사(STT)와 회의록 요약은 모두 로컬에서 동작하는 모델(mlx-whisper, Ollama)로 처리됩니다. 녹음 파일이나 전사 내용이 외부 서버로 전송되지 않아, 민감한 회의 내용이 외부에 유출될 위험이 없습니다.

## 실행 방법

```bash
git clone https://github.com/cocoa030722/AdvancedProjectLab-04.git
cd AdvancedProjectLab-04
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cd APL_04
python manage.py migrate
python manage.py runserver
```

브라우저에서 http://127.0.0.1:8000 접속 후 회원가입하면 바로 써볼 수 있습니다.

위 과정만으로는 녹음 업로드 시 전사·요약이 **가짜(고정) 응답**으로 나옵니다(화면과 흐름은 전부 동일하게 작동). 실제 로컬 LLM/STT로 동작시키려면 아래가 추가로 필요합니다:

- [Ollama](https://ollama.com) 설치 후 `ollama pull qwen2.5:7b`, 환경변수 `OLLAMA_MODEL=qwen2.5:7b`
- (Apple Silicon Mac 전용) `pip install mlx-whisper` + ffmpeg, 환경변수 `MLX_WHISPER_MODEL=mlx-community/whisper-large-v3-turbo`

두 환경변수를 설정하지 않으면 자동으로 가짜 응답 모드로 동작하므로, 화면만 보여줄 목적이라면 생략해도 됩니다.

## 테스트

```bash
cd APL_04
python manage.py test
```
