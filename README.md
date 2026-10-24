# MeetAlign (합의착시탐지기)

회의 녹음을 올리면 회의록을 정리하고, 팀원들이 서로 다르게 이해한 부분을 짚어주는 서비스입니다.

- 녹음 업로드(파일 업로드 + 마이크 직접 녹음) → 네이버 CLOVA Speech로 전사 (매월 무료 20분을 다 쓰거나 호출이 실패하면 로컬 Whisper로 자동 전환, Windows/Mac 모두 지원) → 상용 LLM(Gemini API)으로 회의록 요약 (실패 시 로컬 LLM(Ollama)으로 자동 전환)
- 회의록에서 모호한 표현을 찾아 객관식 이해도 확인 문제를 자동 생성, 주최자가 검토·수정 후 공개
- 정답과 다른 보기를 고른 팀원이 있으면 불일치로 표시하고 근거 문장을 함께 제시
- 익명 질문(질문한 본인만 답변을 볼 수 있음), 팀 초대 코드, 다크 모드 등 지원

## 데이터 프라이버시

회의 녹음 전사(STT)는 네이버 CLOVA Speech를 설정하면 **녹음 파일이 네이버 클라우드 서버로 전송**됩니다. 이번 달 무료 사용량을 다 썼거나 호출이 실패한 녹음, 그리고 CLOVA를 설정하지 않은 경우에는 로컬 Whisper(faster-whisper 또는 mlx-whisper)로 처리되어 녹음 파일이 외부로 나가지 않습니다. 민감한 회의는 CLOVA 설정 없이 로컬 Whisper만으로 실행하는 것을 권장합니다.

회의록 요약, 이해도 확인 문제 생성, 대화창 답변은 상용 LLM 서비스(Gemini API)로 처리되며, 실패 시 로컬 모델(Ollama)을 통해 처리됩니다(`OLLAMA_MODEL`을 설정한 경우).
Gemini를 사용하면 **전사본과 회의록 내용이 Google 서버로 전송**됩니다. 무료 API 키는 입력 데이터가 Google 서비스 개선에 사용될 수 있으므로, 민감한 회의는 Gemini 설정 없이 Ollama만으로 실행하는 것을 권장합니다.

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

위 과정만으로는 녹음 업로드 시 전사·요약이 **가짜(고정) 응답**으로 나옵니다(화면과 흐름은 전부 동일하게 작동). 실제 LLM/STT로 동작시키려면 프로젝트 루트에 `.env` 파일을 만들고 필요한 환경변수를 적습니다(`.env`는 `.gitignore`에 포함되어 커밋되지 않습니다):

```bash
# .env
GEMINI_API_KEY=발급받은_API_키        # https://aistudio.google.com 에서 무료 발급
GEMINI_MODEL=사용할_모델_ID
OLLAMA_MODEL=qwen2.5:7b               # (선택) Gemini 실패 시 사용할 로컬 모델
CLOVA_SPEECH_INVOKE_URL=콘솔의_Invoke_URL   # 네이버 클라우드 CLOVA Speech (아래 STT 설정 참고)
CLOVA_SPEECH_SECRET_KEY=콘솔의_Secret_Key
FASTER_WHISPER_MODEL=large-v3-turbo        # CLOVA 무료 사용량 소진·실패 시 쓸 로컬 Whisper (Windows 포함 모든 OS)
```

프로젝트가 `.env`를 자동으로 읽지 않으므로, 서버 실행 전에 불러옵니다:

```bash
# macOS / Linux / Git Bash
cd APL_04
set -a && source ../.env && set +a
python manage.py runserver
```

```powershell
# Windows PowerShell
cd APL_04
Get-Content ..\.env | Where-Object { $_ -match '^\s*([^#=]+)=([^#]*)' } | ForEach-Object { [Environment]::SetEnvironmentVariable($matches[1].Trim(), $matches[2].Trim(), 'Process') }
python manage.py runserver
```

| 환경변수 | 설명 |
|---|---|
| `GEMINI_API_KEY`, `GEMINI_MODEL` | 둘 다 있으면 Gemini API 사용 (모델 ID는 필수, 기본값 없음) |
| `GEMINI_TIMEOUT` | (선택) Gemini 응답 최대 대기 시간(초), 기본 300 |
| `OLLAMA_MODEL` | Gemini 설정이 없으면 Ollama만 사용, 있으면 Gemini 실패 시 예비로 사용. [Ollama](https://ollama.com) 설치 후 `ollama pull qwen2.5:7b` |
| `CLOVA_SPEECH_INVOKE_URL`, `CLOVA_SPEECH_SECRET_KEY` | 둘 다 있으면 네이버 CLOVA Speech로 전사. 이번 달 무료 사용량이 남아 있을 때만 호출 |
| `CLOVA_SPEECH_FREE_MINUTES` | (선택) 한 달에 CLOVA로 보낼 최대 분량(분), 기본 20(Free 플랜 무료 제공량). 유료(Basic) 플랜이면 크게 올려서 사용 |
| `CLOVA_SPEECH_DIARIZATION` | (선택) `1`이면 화자 구분을 켜서 전사본을 "화자 A: …" 줄로 나눔. Basic 플랜에서는 화자인식이 따로 과금됨 |
| `FASTER_WHISPER_MODEL` | 로컬 Whisper(모든 OS). `pip install faster-whisper`, 예: `large-v3-turbo` (첫 전사 때 약 1.6GB 자동 다운로드). ffmpeg 불필요 |
| `FASTER_WHISPER_DEVICE`, `FASTER_WHISPER_COMPUTE_TYPE` | (선택) 기본 `auto`(NVIDIA GPU가 되면 GPU, 아니면 CPU) / `default`. CPU만 쓸 때는 `int8`이 빠름 |
| `MLX_WHISPER_MODEL` | (Apple Silicon Mac 전용, 설정 시 faster-whisper보다 우선) `pip install mlx-whisper` + ffmpeg, 예: `mlx-community/whisper-large-v3-turbo` |

LLM/STT 환경변수를 하나도 설정하지 않으면 자동으로 가짜 응답 모드로 동작하므로, 화면만 보여줄 목적이라면 생략해도 됩니다.

> Gemini 무료 등급은 응답에 수십 초~몇 분이 걸리거나, 요청이 몰릴 때 일시적 오류(429/503)가 날 수 있습니다. 일시적 오류는 자동으로 재시도하고, 그래도 실패하면 Ollama(설정 시) 또는 가짜 응답으로 대체됩니다.

### STT(전사) 설정

전사 엔진은 다음 순서로 고릅니다. 회의 상세 화면의 녹음 업로드 폼에 지금 어떤 엔진이 쓰이는지와 이번 달 CLOVA 무료 사용량이 표시됩니다.

1. **CLOVA Speech** — `CLOVA_SPEECH_INVOKE_URL`과 `CLOVA_SPEECH_SECRET_KEY`가 있고, 이번 달 사용량 + 이 녹음 길이가 `CLOVA_SPEECH_FREE_MINUTES`(기본 20분) 안에 들어올 때
2. **로컬 Whisper** — CLOVA 무료 사용량을 다 썼거나, CLOVA 호출이 실패했거나(키 오류, 네트워크, 서버 오류), CLOVA를 설정하지 않았을 때. `MLX_WHISPER_MODEL`(Mac) 또는 `FASTER_WHISPER_MODEL`(모든 OS)
3. 둘 다 없으면 가짜 응답 모드

**CLOVA Speech 준비** ([네이버 클라우드 플랫폼](https://www.ncloud.com) 콘솔)

1. Services > AI Services > CLOVA Speech에서 이용 신청 후 **도메인 생성**. 서비스 플랜은 **Free**(계정당 1개, 매월 20분 무료, 삭제 불가)를 고르면 됩니다. 결과 저장용 Object Storage 버킷이 하나 필요합니다.
2. 만든 도메인의 **빌더 > 설정 > 연동 정보**에서 `Secret Key`와 `Invoke URL`을 복사해 `.env`에 넣습니다.
3. 무료 제공량은 네이버 클라우드가 15초 단위로 올림해 계산하므로, 이 프로젝트도 같은 단위로 사용량을 기록합니다(`SttUsage` 테이블, 매월 1일 초기화). 길이를 미리 알 수 없는 파일(브라우저 마이크 녹음 등)은 호출 뒤 응답으로 사용량을 기록하므로 마지막 한 건은 한도를 조금 넘길 수 있습니다. 무료 플랜의 정확한 제공량과 초과 시 동작은 콘솔의 요금 안내에서 확인하세요.
4. 실측(Free 도메인): 24초 음성을 2.5초에 전사했습니다. 다만 합성음 테스트에서 사람 이름 하나가 통째로 빠진 적이 있으니(같은 음성을 Whisper는 정확히 전사), 실제 회의 녹음으로 품질을 한 번 확인해 보세요. 장부(`SttUsage`)는 이 앱이 보낸 호출만 세므로, 콘솔이나 다른 도구로 직접 호출한 분량은 포함되지 않습니다.

**로컬 Whisper 준비 (Windows / Linux / Intel Mac)**

```bash
pip install faster-whisper "av<19"  # 모델은 첫 전사 때 자동 다운로드 (large-v3-turbo 약 1.6GB)
# NVIDIA GPU로 돌리려면 (선택) — 설치만 하면 DLL 경로는 자동으로 잡힙니다
pip install nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*"
```

> `"av<19"`: PyPI의 faster-whisper 1.2.1은 PyAV 19에서 제거된 인자를 아직 쓰고 있어서, 그냥 `pip install faster-whisper`만 하면 전사할 때 `open() got an unexpected keyword argument 'metadata_errors'`로 실패합니다(2026-10 기준). 수정이 들어간 faster-whisper 다음 버전이 나오면 고정을 풀어도 됩니다.

`.env`에 `FASTER_WHISPER_MODEL=large-v3-turbo`를 넣으면 됩니다. GPU가 없거나 CUDA 설정이 안 되면 `FASTER_WHISPER_DEVICE=cpu FASTER_WHISPER_COMPUTE_TYPE=int8`로 CPU에서 돌릴 수 있습니다. 모델이 클수록 정확하지만 느리므로, CPU만 있는 PC에서 긴 회의를 자주 전사한다면 `small`이나 `medium`도 고려해 보세요. 실측(Windows 노트북, RTX 4060 Laptop): `large-v3-turbo`는 GPU에서 24초 음성을 모델 로딩 포함 7초에, `base`는 CPU int8로 4초에 전사했습니다. 첫 실행은 모델 다운로드(1.6GB) 때문에 몇 분 더 걸립니다. Apple Silicon Mac은 기존처럼 `MLX_WHISPER_MODEL`을 쓰는 편이 빠릅니다.

> 새 마이그레이션(0019, CLOVA 사용량 기록 테이블)이 추가되었으니 `git pull` 후 서버를 켜기 전에 `python manage.py migrate`를 먼저 실행하세요.

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

테스트는 실제 LLM/STT를 호출하지 않으므로, 위 환경변수가 설정돼 있어도 그대로 실행하면 됩니다.
