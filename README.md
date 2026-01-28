# CloudRAG

Gemini LLM 기반 클라우드 드라이브 RAG 챗봇

## 기능

- **Google Drive, OneDrive, Notion 연동**
- **RAG 기반 문서 검색 및 Q&A**
- **Gemini LLM을 활용한 문서 요약**
- **PDF, DOCX, XLSX, PPTX 지원**

## 설치

```bash
# 1. 가상환경 생성
python -m venv venv

# Windows
venv\Scripts\activate

# macOS/Linux
source venv/bin/activate

# 2. 패키지 설치
pip install -r requirements.txt

# 3. 환경 설정
cp .env.example .env
# .env 파일을 편집하여 API 키 설정
```

## 환경 설정

`.env` 파일에 다음 값들을 설정:

### 필수
- `GEMINI_API_KEY`: Google AI Studio에서 발급
- `SESSION_SECRET`: 임의의 보안 문자열

### 선택 (연동할 서비스만)
- **Google Drive**: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`
- **OneDrive**: `ONEDRIVE_CLIENT_ID`, `ONEDRIVE_CLIENT_SECRET`
- **Notion**: `NOTION_CLIENT_ID`, `NOTION_CLIENT_SECRET`

## 실행

```bash
# 방법 1: main.py 사용
python main.py

# 방법 2: uvicorn 직접 실행
cd backend
uvicorn app:app --reload --host 0.0.0.0 --port 8000
```

브라우저에서 http://localhost:8000 접속

## API 엔드포인트

### 인증
- `POST /auth/login` - 로그인
- `POST /auth/logout` - 로그아웃
- `POST /auth/signup` - 회원가입
- `GET /me` - 현재 사용자 정보

### OAuth 연동
- `GET /auth/google/login` - Google Drive 연동
- `GET /auth/onedrive/login` - OneDrive 연동
- `GET /auth/notion/login` - Notion 연동

### 채팅
- `POST /chat/ask` - 통합 Q&A
- `POST /chat/google/ask` - Google Drive 기반 Q&A
- `POST /chat/onedrive/ask` - OneDrive 기반 Q&A
- `POST /chat/notion/ask` - Notion 기반 Q&A
- `POST /chat/auto/ask` - 자동 검색 Q&A
- `POST /chat/summarize` - 문서 요약

### 연결 관리
- `GET /connections` - 연결된 서비스 목록
- `DELETE /connections/{provider}` - 연결 해제

## 프로젝트 구조

```
Portfolio_Chatbot/
├── backend/
│   ├── app.py              # FastAPI 메인 앱
│   ├── storage.py          # SQLite DB 관리
│   ├── providers.py        # 드라이브 연동
│   ├── chat_router.py      # RAG 챗봇 라우터
│   ├── llm_gemini.py       # Gemini LLM 연동
│   ├── gdrive/
│   │   └── auth_google.py  # Google OAuth
│   ├── onedrive/
│   │   └── auth_onedrive.py # OneDrive OAuth
│   ├── notion/
│   │   └── auth_notion.py  # Notion OAuth
│   ├── auth/
│   │   └── signup.py       # 회원가입
│   └── static/
│       ├── login.html      # 로그인 페이지
│       └── ui.html         # 메인 UI
├── data/
│   └── media/              # 미디어 저장소
├── main.py                 # 서버 실행
├── requirements.txt        # Python 패키지
├── .env.example           # 환경 설정 예시
└── README.md
```

## 기술 스택

- **Backend**: FastAPI, Python 3.10+
- **Database**: SQLite
- **LLM**: Google Gemini
- **OAuth**: Google, Microsoft, Notion
- **PDF**: PyMuPDF, pdfminer
