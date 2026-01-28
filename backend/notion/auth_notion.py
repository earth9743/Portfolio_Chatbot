# backend/notion/auth_notion.py
# CloudRAG - Notion OAuth
import os
import time
from typing import Optional
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Query
from starlette.responses import RedirectResponse

from storage import save_connection, get_connection_row

router = APIRouter(prefix="/auth/notion", tags=["notion"])

# ===== 환경변수 =====
DEFAULT_REDIRECT = "http://localhost:8000/auth/notion/callback"
AUTH_URL = "https://api.notion.com/v1/oauth/authorize"
TOKEN_URL = "https://api.notion.com/v1/oauth/token"
NOTION_VERSION = "2022-06-28"


def _env():
    cid = os.getenv("NOTION_CLIENT_ID", "")
    csec = os.getenv("NOTION_CLIENT_SECRET", "")
    ruri = os.getenv("NOTION_REDIRECT_URI", DEFAULT_REDIRECT)
    return cid, csec, ruri


# ===== OAuth Flow =====
@router.get("/login")
async def login(user_name: str = Query(...)):
    """Notion OAuth 로그인 시작"""
    cid, _, ruri = _env()
    if not cid:
        raise HTTPException(status_code=500, detail="NOTION_CLIENT_ID not configured")

    params = {
        "client_id": cid,
        "redirect_uri": ruri,
        "response_type": "code",
        "owner": "user",
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
        raise HTTPException(status_code=500, detail="NOTION_CLIENT_ID/SECRET not configured")

    # Notion은 Basic Auth로 토큰 교환
    import base64
    auth_str = base64.b64encode(f"{cid}:{csec}".encode()).decode()

    headers = {
        "Authorization": f"Basic {auth_str}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_VERSION,
    }

    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": ruri,
    }

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(TOKEN_URL, json=data, headers=headers)

    if r.status_code != 200:
        raise HTTPException(status_code=400, detail=f"Token exchange failed: {r.text}")

    tok = r.json()

    access_token = tok.get("access_token")
    if not access_token:
        raise HTTPException(status_code=400, detail="No access_token in response")

    # Notion 응답에서 workspace/owner 정보 추출
    owner = tok.get("owner", {})
    workspace = tok.get("workspace_name", "")

    # owner가 user인 경우 이메일 추출
    email = ""
    account_id = ""
    if owner.get("type") == "user":
        user_info = owner.get("user", {})
        email = user_info.get("person", {}).get("email", "")
        account_id = user_info.get("id", "")

    # 저장 (Notion은 expires_at 없음 - 영구 토큰)
    save_connection(
        user_name=state,
        provider="notion",
        token={"access_token": access_token},
        provider_account_id=account_id,
        provider_account_email=email,
        meta={
            "login_at": int(time.time()),
            "workspace_name": workspace,
            "workspace_id": tok.get("workspace_id", ""),
            "bot_id": tok.get("bot_id", ""),
        },
    )

    return RedirectResponse("/ui", status_code=302)


# ===== Search API =====
@router.get("/pages")
async def list_pages(
    user_name: str = Query(...),
    q: str = Query(""),
    limit: int = Query(30),
    cursor: str = Query(""),
):
    """Notion 페이지 검색"""
    row = get_connection_row(user_name, "notion")
    if not row:
        raise HTTPException(status_code=401, detail="Notion not connected")

    access_token = row.get("access_token")
    if not access_token:
        raise HTTPException(status_code=401, detail="No access_token")

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }

    payload = {
        "query": q or "",
        "page_size": min(limit, 100),
        "sort": {"direction": "descending", "timestamp": "last_edited_time"},
    }
    if cursor:
        payload["start_cursor"] = cursor

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post("https://api.notion.com/v1/search", headers=headers, json=payload)

    if r.status_code == 401:
        raise HTTPException(status_code=401, detail="Token invalid. Please reconnect Notion.")

    if r.status_code != 200:
        raise HTTPException(status_code=400, detail=f"Notion API error: {r.text}")

    data = r.json()
    items = []

    for result in data.get("results", []):
        url = result.get("url", "")
        last_edited = result.get("last_edited_time", "")
        title = "제목 없음"

        try:
            if result.get("object") == "page":
                props = result.get("properties", {})
                for _, v in props.items():
                    if v and v.get("type") == "title":
                        t = v.get("title", [])
                        if t and t[0].get("plain_text"):
                            title = t[0]["plain_text"]
                            break
            elif result.get("object") == "database":
                tt = result.get("title", [])
                if tt and tt[0].get("plain_text"):
                    title = tt[0]["plain_text"]
        except Exception:
            pass

        items.append({
            "id": result.get("id", ""),
            "title": title,
            "url": url,
            "last_edited_time": last_edited,
            "object": result.get("object", "page"),
        })

    return {
        "items": items,
        "has_more": data.get("has_more", False),
        "next_cursor": data.get("next_cursor", ""),
    }


# ===== Debug =====
@router.get("/_debug/env")
async def debug_env():
    cid, csec, ruri = _env()
    return {
        "CLIENT_ID_len": len(cid),
        "CLIENT_SECRET_len": len(csec),
        "REDIRECT_URI": ruri,
    }
