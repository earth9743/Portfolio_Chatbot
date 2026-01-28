# backend/onedrive/auth_onedrive.py
# CloudRAG - OneDrive OAuth
import os
import time
from typing import Optional
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Query
from starlette.responses import RedirectResponse

from storage import save_connection, get_connection_row

router = APIRouter(prefix="/auth/onedrive", tags=["onedrive"])

# ===== 환경변수 =====
DEFAULT_REDIRECT = "http://localhost:8000/auth/onedrive/callback"
TENANT = os.getenv("ONEDRIVE_TENANT", "common")
AUTH_URL = f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/authorize"
TOKEN_URL = f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"
GRAPH_ME = "https://graph.microsoft.com/v1.0/me"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"


def _env():
    cid = os.getenv("ONEDRIVE_CLIENT_ID", "")
    csec = os.getenv("ONEDRIVE_CLIENT_SECRET", "")
    ruri = os.getenv("ONEDRIVE_REDIRECT_URI", DEFAULT_REDIRECT)
    return cid, csec, ruri


def _scopes() -> list[str]:
    s = os.getenv("ONEDRIVE_SCOPES")
    if s:
        return [x for x in s.split() if x]
    return ["offline_access", "Files.Read", "User.Read", "openid", "email", "profile"]


# ===== OAuth Flow =====
@router.get("/login")
async def login(user_name: str = Query(...)):
    """OneDrive OAuth 로그인 시작"""
    cid, _, ruri = _env()
    if not cid:
        raise HTTPException(status_code=500, detail="ONEDRIVE_CLIENT_ID not configured")

    params = {
        "client_id": cid,
        "response_type": "code",
        "redirect_uri": ruri,
        "response_mode": "query",
        "scope": " ".join(_scopes()),
        "state": user_name,
        "prompt": "consent",
    }
    return RedirectResponse(f"{AUTH_URL}?{urlencode(params)}", status_code=302)


@router.get("/callback")
async def callback(code: str, state: str, error: Optional[str] = None):
    """OAuth 콜백 처리"""
    if error:
        raise HTTPException(status_code=400, detail=f"OAuth error: {error}")

    cid, csec, ruri = _env()
    if not cid or not csec:
        raise HTTPException(status_code=500, detail="ONEDRIVE_CLIENT_ID/SECRET not configured")

    # 토큰 교환
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": ruri,
        "client_id": cid,
        "client_secret": csec,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(TOKEN_URL, data=data, headers=headers)

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
    params = {"$select": "id,displayName,mail,userPrincipalName"}
    async with httpx.AsyncClient(timeout=20) as client:
        me = await client.get(GRAPH_ME, params=params, headers={"Authorization": f"Bearer {access_token}"})

    me_json = me.json() if me.status_code == 200 else {}
    account_id = me_json.get("id", "")
    email = me_json.get("mail") or me_json.get("userPrincipalName", "")

    # 저장
    save_connection(
        user_name=state,
        provider="onedrive",
        token=tok,
        provider_account_id=account_id,
        provider_account_email=email,
        meta={"login_at": int(time.time()), "display_name": me_json.get("displayName", "")},
    )

    return RedirectResponse("/ui", status_code=302)


# ===== Files API =====
@router.get("/files")
async def list_files(
    user_name: str = Query(...),
    q: str = Query(""),
    folderId: str = Query("root"),
    next_link: str = Query("", alias="next"),
):
    """OneDrive 파일 목록"""
    row = get_connection_row(user_name, "onedrive")
    if not row:
        raise HTTPException(status_code=401, detail="OneDrive not connected")

    access_token = row.get("access_token")
    if not access_token:
        raise HTTPException(status_code=401, detail="No access_token")

    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}

    # URL 결정
    if next_link:
        url = next_link
        params = {}
    elif q:
        url = f"{GRAPH_BASE}/me/drive/root/search(q='{q}')"
        params = {}
    elif folderId and folderId != "root":
        url = f"{GRAPH_BASE}/me/drive/items/{folderId}/children"
        params = {}
    else:
        url = f"{GRAPH_BASE}/me/drive/root/children"
        params = {}

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url, headers=headers, params=params)

    if r.status_code == 401:
        raise HTTPException(status_code=401, detail="Token expired. Please reconnect OneDrive.")

    if r.status_code != 200:
        raise HTTPException(status_code=400, detail=f"Graph API error: {r.text}")

    return r.json()


# ===== Debug =====
@router.get("/_debug/env")
async def debug_env():
    cid, csec, ruri = _env()
    return {
        "CLIENT_ID_len": len(cid),
        "CLIENT_SECRET_len": len(csec),
        "REDIRECT_URI": ruri,
        "TENANT": TENANT,
        "SCOPES": _scopes(),
    }
