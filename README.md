# MeetAlign (합의착시탐지기)

회의 녹음을 올리면 회의록을 정리하고, 팀원들이 서로 다르게 이해한 부분을 짚어주는 서비스입니다.

- 녹음 업로드(파일 업로드 + 마이크 직접 녹음) → 로컬 STT(mlx-whisper)로 전사 → 로컬 LLM(Ollama)으로 회의록 요약
- 회의록에서 모호한 표현을 찾아 객관식 이해도 확인 문제를 자동 생성, 주최자가 검토·수정 후 공개
- 정답과 다른 보기를 고른 팀원이 있으면 불일치로 표시하고 근거 문장을 함께 제시
- 익명 질문(질문한 본인만 답변을 볼 수 있음), 팀 초대 코드, 다크 모드 등 지원

## 데이터 프라이버시

회의 녹음 전사(STT)는 로컬에서 동작하는 모델(mlx-whisper, Ollama)로 처리됩니다.

회의록 요약 및 이해도 확인 문제 생성은 상용 LLM 서비스(GEMINI)로 처리되며 실패 시, 로컬 모델(Ollama)를 통해 처리됩니다.

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

## 다른 기기(다른 OS 포함)에서 접속하기

마이크로 직접 녹음하는 기능(`getUserMedia`)은 브라우저 보안 정책상 `https://` 또는 `localhost`에서만 동작합니다. 같은 와이파이의 다른 기기(윈도우 포함)에서 `http://<내부 IP>:8000`처럼 일반 http로 접속하면 화면은 다 보이지만 마이크 녹음 버튼만 안 됩니다(파일 업로드는 정상 작동).

마이크 녹음까지 되게 하려면 [Cloudflare Tunnel](https://github.com/cloudflare/cloudflared)(무료, 계정 불필요)로 https 주소를 만들면 됩니다:

```bash
brew install cloudflared   # 또는 다른 OS용 설치 방법 참고
cloudflared tunnel --url http://localhost:8000
```

터미널에 뜨는 `https://*.trycloudflare.com` 주소로 접속하면 됩니다. 이때 서버를 아래 환경변수와 함께 실행해야 합니다(안 그러면 접속이 막히거나 로그인 시 CSRF 오류가 납니다):

```bash
DJANGO_ALLOWED_HOSTS="localhost,127.0.0.1,.trycloudflare.com" \
DJANGO_CSRF_TRUSTED_ORIGINS="https://*.trycloudflare.com" \
python manage.py runserver 0.0.0.0:8000
```

## 테스트

```bash
cd APL_04
python manage.py test
```
