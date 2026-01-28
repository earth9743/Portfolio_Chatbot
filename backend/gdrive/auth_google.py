# backend/gdrive/auth_google.py
# CloudRAG - Google Drive OAuth
import os
import time
from typing import Optional
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Query
from starlette.responses import RedirectResponse

from storage import save_connection, get_connection_row

router = APIRouter(prefix="/auth/google", tags=["google"])

# ===== 환경변수 =====
DEFAULT_REDIRECT = "http://localhost:8000/auth/google/callback"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"


def _env():
    cid = os.getenv("GOOGLE_CLIENT_ID", "")
    csec = os.getenv("GOOGLE_CLIENT_SECRET", "")
    ruri = os.getenv("GOOGLE_REDIRECT_URI", DEFAULT_REDIRECT)
    return cid, csec, ruri


def _scopes() -> list[str]:
    s = os.getenv("GOOGLE_SCOPES")
    if s:
        return [x for x in s.split() if x]
    return [
        "openid",
        "email",
        "profile",
        "https://www.googleapis.com/auth/drive.readonly",
    ]


# ===== OAuth Flow =====
@router.get("/login")
async def login(user_name: str = Query(...)):
    """Google OAuth 로그인 시작"""
    cid, _, ruri = _env()
    if not cid:
        raise HTTPException(status_code=500, detail="GOOGLE_CLIENT_ID not configured")

    params = {
        "client_id": cid,
        "redirect_uri": ruri,
        "response_type": "code",
        "scope": " ".join(_scopes()),
        "access_type": "offline",
        "prompt": "consent",
        "state": user_name,
    }
    return RedirectResponse(f"{AUTH_URL}?{urlencode(params)}", status_code=302)


@router.get("/callback")
async def callback(code: str, state: str, error: Optional[str] = None):
    """OAuth 콜백 처리"""
    if error:
        raise HTTPException(status_code=400, detail=f"OAuth error: {error}")

    cid, csec, ruri = _env()
    if not cid or not csec:
        raise HTTPException(status_code=500, detail="GOOGLE_CLIENT_ID/SECRET not configured")

    # 토큰 교환
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": ruri,
        "client_id": cid,
        "client_secret": csec,
    }

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(TOKEN_URL, data=data)

    if r.status_code != 200:
        raise HTTPException(status_code=400, detail=f"Token exchange failed: {r.text}")

    tok = r.json()

    # expires_at 계산
    if tok.get("expires_in"):
        tok["expires_at"] = int(time.time()) + int(tok["expires_in"]) - 60

    access_token = tok.get("access_token")
    if not access_token:
        raise HTTPException(status_code=400, detail="No access_token in response")

    # 사용자 정보 조회
    async with httpx.AsyncClient(timeout=15) as client:
        me = await client.get(USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"})

    me_json = me.json() if me.status_code == 200 else {}
    email = me_json.get("email", "")
    account_id = me_json.get("id", "")

    # 저장
    save_connection(
        user_name=state,
        provider="google",
        token=tok,
        provider_account_id=account_id,
        provider_account_email=email,
        meta={"login_at": int(time.time()), "name": me_json.get("name", "")},
    )

    return RedirectResponse("/ui", status_code=302)


# ===== Files API =====
@router.get("/files")
async def list_files(
    user_name: str = Query(...),
    q: str = Query(""),
    folderId: str = Query("root"),
    pageToken: str = Query(""),
):
    """Google Drive 파일 목록"""
    row = get_connection_row(user_name, "google")
    if not row:
        raise HTTPException(status_code=401, detail="Google not connected")

    access_token = row.get("access_token")
    if not access_token:
        raise HTTPException(status_code=401, detail="No access_token")

    # 쿼리 구성
    query_parts = ["trashed = false"]
    if folderId and folderId != "root":
        query_parts.append(f"'{folderId}' in parents")
    if q:
        query_parts.append(f"name contains '{q}'")

    url = "https://www.googleapis.com/drive/v3/files"
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {
        "q": " and ".join(query_parts),
        "pageSize": 50,
        "fields": "nextPageToken,files(id,name,mimeType,size,modifiedTime,webViewLink)",
        "orderBy": "modifiedTime desc",
    }
    if pageToken:
        params["pageToken"] = pageToken

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url, headers=headers, params=params)

    if r.status_code == 401:
        # 토큰 만료 - 갱신 필요
        raise HTTPException(status_code=401, detail="Token expired. Please reconnect Google Drive.")

    if r.status_code != 200:
        raise HTTPException(status_code=400, detail=f"Drive API error: {r.text}")

    return r.json()


# ===== Debug =====
@router.get("/_debug/env")
async def debug_env():
    cid, csec, ruri = _env()
    return {
        "CLIENT_ID_len": len(cid),
        "CLIENT_SECRET_len": len(csec),
        "REDIRECT_URI": ruri,
        "SCOPES": _scopes(),
    }
