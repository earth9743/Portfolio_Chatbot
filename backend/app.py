# backend/app.py
# CloudRAG - 메인 애플리케이션
import os
import sqlite3

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

# 환경 로드
load_dotenv()

# 모듈 임포트
import storage
from storage import (
    init_db,
    verify_login,
    get_user,
    get_all_connections,
    get_connection_row,
    get_connection_meta,
    update_connection_meta,
    DB_PATH,
)

# 라우터 임포트
from chat_router import router as chat_router
from gdrive.auth_google import router as google_oauth_router
from onedrive.auth_onedrive import router as onedrive_oauth_router
from notion.auth_notion import router as notion_oauth_router
from auth.signup import router as signup_router

# ===================================================
# 경로 설정
# ===================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
LOGIN_HTML = os.path.join(STATIC_DIR, "login.html")
UI_HTML = os.path.join(STATIC_DIR, "ui.html")

# ===================================================
# FastAPI 앱 초기화
# ===================================================
app = FastAPI(
    title="CloudRAG",
    description="Gemini LLM 기반 클라우드 드라이브 RAG 챗봇",
    version="1.0.0"
)

# 세션 미들웨어
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET", "cloudrag-dev-secret"),
    session_cookie="cloudrag_session",
    max_age=60 * 60 * 8,  # 8시간
    same_site="lax",
    https_only=False,
)

# DB 초기화
init_db()

# 라우터 등록
app.include_router(chat_router)
app.include_router(google_oauth_router)
app.include_router(onedrive_oauth_router)
app.include_router(notion_oauth_router)
app.include_router(signup_router)

# 정적 파일 마운트
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
os.makedirs(storage.MEDIA_ROOT, exist_ok=True)
app.mount("/media", StaticFiles(directory=storage.MEDIA_ROOT, check_dir=False), name="media")

# ===================================================
# 기본 라우트
# ===================================================
@app.get("/health")
def health():
    """헬스 체크"""
    return {"ok": True, "service": "CloudRAG"}


@app.get("/", include_in_schema=False)
def root(request: Request):
    """루트 - 로그인 상태면 UI로, 아니면 로그인으로"""
    if request.session.get("user"):
        return RedirectResponse("/ui", status_code=302)
    return RedirectResponse("/login", status_code=302)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, force: int = Query(0)):
    """로그인 페이지"""
    if request.session.get("user") and not force:
        return RedirectResponse("/ui", status_code=302)
    if not os.path.exists(LOGIN_HTML):
        return HTMLResponse("<h1>login.html not found</h1>", status_code=404)
    return FileResponse(LOGIN_HTML)


@app.get("/ui", response_class=HTMLResponse)
def ui_page(request: Request):
    """메인 UI 페이지"""
    user = request.session.get("user")
    if not user:
        return RedirectResponse("/login?force=1", status_code=302)
    if not os.path.exists(UI_HTML):
        return HTMLResponse("<h1>ui.html not found</h1>", status_code=404)
    return FileResponse(UI_HTML)


# ===================================================
# 인증 API
# ===================================================
@app.post("/auth/login")
async def auth_login(request: Request):
    """로그인"""
    form = await request.form()
    user_name = (form.get("user_name") or "").strip()
    password = form.get("password") or ""

    if verify_login(user_name, password):
        request.session["user"] = user_name
        return RedirectResponse("/ui", status_code=303)

    return JSONResponse({"detail": "아이디/비밀번호를 확인하세요."}, status_code=400)


@app.post("/auth/logout")
async def auth_logout(request: Request):
    """로그아웃"""
    request.session.clear()
    return JSONResponse({"ok": True})


@app.get("/logout")
async def logout_get(request: Request):
    """로그아웃 (GET)"""
    request.session.clear()
    return RedirectResponse("/login?force=1", status_code=302)


@app.get("/me")
def me(request: Request):
    """현재 로그인 사용자 정보"""
    user = request.session.get("user")
    if not user:
        return JSONResponse({"detail": "not logged in"}, status_code=401)

    user_info = get_user(user)
    return {
        "user_name": user,
        "display_name": user_info.get("display_name", "") if user_info else "",
        "email": user_info.get("email", "") if user_info else "",
    }


# ===================================================
# 연결 상태 API
# ===================================================
@app.get("/connections")
def get_connections(request: Request):
    """연결된 클라우드 서비스 목록"""
    user = request.session.get("user")
    if not user:
        return JSONResponse({"detail": "not logged in"}, status_code=401)

    connections = get_all_connections(user)
    providers = {c["provider"]: c.get("provider_account_email", "") for c in connections}

    return {
        "google": "google" in providers,
        "onedrive": "onedrive" in providers,
        "notion": "notion" in providers,
        "google_email": providers.get("google", ""),
        "onedrive_email": providers.get("onedrive", ""),
        "notion_email": providers.get("notion", ""),
    }


@app.delete("/connections/{provider}")
def disconnect_provider(provider: str, request: Request):
    """클라우드 서비스 연결 해제"""
    user = request.session.get("user")
    if not user:
        return JSONResponse({"detail": "not logged in"}, status_code=401)

    from storage import delete_connection
    delete_connection(user, provider)
    return {"ok": True, "message": f"{provider} 연결이 해제되었습니다"}


# ===================================================
# 빠른 링크 API
# ===================================================
@app.get("/connections/quick-link")
def get_quick_link(request: Request, provider: str = Query(...)):
    """빠른 링크 조회"""
    user = request.session.get("user")
    if not user:
        return JSONResponse({"detail": "not logged in"}, status_code=401)

    row = get_connection_row(user, provider)
    if not row:
        raise HTTPException(status_code=404, detail="연결되지 않은 서비스입니다")

    meta = get_connection_meta(user, provider) or {}
    return {"url": meta.get("quick_link", "")}


@app.post("/connections/quick-link")
async def set_quick_link(request: Request):
    """빠른 링크 설정"""
    user = request.session.get("user")
    if not user:
        return JSONResponse({"detail": "not logged in"}, status_code=401)

    data = await request.json()
    provider = data.get("provider", "").lower()
    url = (data.get("url") or "").strip()

    row = get_connection_row(user, provider)
    if not row:
        raise HTTPException(status_code=404, detail="연결되지 않은 서비스입니다")

    if url and not (url.startswith("http://") or url.startswith("https://")):
        url = "https://" + url

    update_connection_meta(user, provider, {"quick_link": url})
    return {"ok": True, "url": url}


# ===================================================
# 디버그 API
# ===================================================
@app.get("/__routes")
def list_routes():
    """등록된 라우트 목록"""
    return [{"path": r.path, "methods": list(r.methods or [])} for r in app.router.routes]


@app.get("/__env")
def check_env():
    """환경변수 상태 확인 (길이만 표시)"""
    return {
        "GEMINI_API_KEY": len(os.getenv("GEMINI_API_KEY", "")),
        "GOOGLE_CLIENT_ID": len(os.getenv("GOOGLE_CLIENT_ID", "")),
        "GOOGLE_CLIENT_SECRET": len(os.getenv("GOOGLE_CLIENT_SECRET", "")),
        "ONEDRIVE_CLIENT_ID": len(os.getenv("ONEDRIVE_CLIENT_ID", "")),
        "ONEDRIVE_CLIENT_SECRET": len(os.getenv("ONEDRIVE_CLIENT_SECRET", "")),
        "NOTION_CLIENT_ID": len(os.getenv("NOTION_CLIENT_ID", "")),
        "NOTION_CLIENT_SECRET": len(os.getenv("NOTION_CLIENT_SECRET", "")),
        "SESSION_SECRET": len(os.getenv("SESSION_SECRET", "")),
    }


# ===================================================
# 엔트리포인트
# ===================================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
