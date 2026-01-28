# backend/providers.py
# CloudRAG - 클라우드 드라이브 연동 (Google Drive, OneDrive, Notion)
# -*- coding: utf-8 -*-

import os
import re
import io
import time
import json
import base64
import logging
import asyncio
import zipfile
import tempfile
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode, urlparse, parse_qs

import httpx

from storage import DB_PATH, get_provider_token, save_connection

LOG = logging.getLogger("uvicorn.error")

# ===================== 공통 상수 =====================

MAX_PREVIEW_BYTES = 8 * 1024 * 1024  # 8MB
MAX_PREVIEW_RANGE = 2 * 1024 * 1024  # 2MB
PREVIEW_HARD_CAP_MB = int(os.getenv("PREVIEW_HARD_CAP_MB", "32"))
MAX_HARD_BYTES = max(1, PREVIEW_HARD_CAP_MB) * 1024 * 1024
_DL_SEMAPHORE = asyncio.Semaphore(4)


# ===================== 공통 유틸 =====================

def _json_load_maybe(raw: Any) -> Dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="ignore")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return {}
    if isinstance(raw, dict):
        return raw
    return {}


def _read_token_row(user_name: str, provider: str) -> dict:
    row = get_provider_token(user_name, provider) or {}
    if isinstance(row, dict):
        return row
    try:
        return dict(row)
    except Exception:
        return {}


def _extract_tokens(row: dict) -> dict:
    if not row:
        return {}
    if any(k in row for k in ("access_token", "refresh_token", "expires_at")):
        return {
            "access_token": row.get("access_token"),
            "refresh_token": row.get("refresh_token"),
            "expires_at": int(row.get("expires_at") or 0),
            "scope": row.get("scope"),
            "token_type": row.get("token_type") or "Bearer",
        }
    meta = _json_load_maybe(row.get("meta_json"))
    return {
        "access_token": meta.get("access_token"),
        "refresh_token": meta.get("refresh_token"),
        "expires_at": int(meta.get("expires_at") or 0),
        "scope": meta.get("scope"),
        "token_type": meta.get("token_type") or "Bearer",
    }


async def _http_get_bytes(url: str, headers: dict = None, timeout: int = 60) -> bytes:
    """URL에서 바이트 다운로드"""
    async with _DL_SEMAPHORE:
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
                r = await c.get(url, headers=headers or {})
                return r.content if r.status_code == 200 else b""
        except Exception:
            return b""


# ===================== 텍스트 추출 유틸 =====================

_OFFICE_EXTS = {".docx", ".pptx", ".xlsx"}


def _is_office_zip(name: str) -> bool:
    lower = (name or "").lower()
    return any(lower.endswith(ext) for ext in _OFFICE_EXTS)


def _extract_xml_text(xml_bytes: bytes, tag: str) -> str:
    import html as _html
    try:
        txt = xml_bytes.decode("utf-8", "ignore")
        parts = re.findall(fr"<{tag}[^>]*>(.*?)</{tag}>", txt, flags=re.S | re.I)
        joined = " ".join(_html.unescape(re.sub(r"<[^>]+>", "", s)) for s in parts)
        return re.sub(r"\s+", " ", joined).strip()
    except Exception:
        return ""


def _preview_office_zip(filename: str, data: bytes, max_chars: int = 600) -> str:
    """Office 파일에서 텍스트 추출"""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            name_lower = (filename or "").lower()
            text = ""
            if name_lower.endswith(".docx"):
                for candidate in ["word/document.xml"]:
                    if candidate in zf.namelist():
                        text += " " + _extract_xml_text(zf.read(candidate), "w:t")
            elif name_lower.endswith(".pptx"):
                for nm in sorted([n for n in zf.namelist() if n.startswith("ppt/slides/slide") and n.endswith(".xml")]):
                    text += " " + _extract_xml_text(zf.read(nm), "a:t")
            elif name_lower.endswith(".xlsx"):
                if "xl/sharedStrings.xml" in zf.namelist():
                    text = _extract_xml_text(zf.read("xl/sharedStrings.xml"), "t")
            text = (text or "").strip()
            if text:
                return text[:max_chars] + (" ..." if len(text) > max_chars else "")
    except Exception:
        pass
    return f"[binary {len(data)} bytes]"


def _preview_pdf_bytes(data: bytes, max_pages: int = 3, max_chars: int = 900) -> str:
    """PDF에서 텍스트 추출"""
    # PyMuPDF 시도
    try:
        import fitz
        doc = fitz.open(stream=data, filetype="pdf")
        texts = []
        for i, page in enumerate(doc):
            if i >= max_pages:
                break
            t = page.get_text()
            texts.append(t or "")
        txt = " ".join(texts).strip()
        txt = re.sub(r"\s+", " ", txt)
        if txt:
            return txt[:max_chars] + (" ..." if len(txt) > max_chars else "")
    except Exception:
        pass

    # pdfminer 폴백
    try:
        from pdfminer.high_level import extract_text_to_fp
        from pdfminer.layout import LAParams
        buf = io.StringIO()
        extract_text_to_fp(io.BytesIO(data), buf, laparams=LAParams(), maxpages=max_pages)
        txt = buf.getvalue()
        txt = re.sub(r"\s+", " ", txt).strip()
        if txt:
            return txt[:max_chars] + (" ..." if len(txt) > max_chars else "")
    except Exception:
        pass

    return f"[binary {len(data)} bytes]"


def text_preview(title: str, data: bytes, max_chars: int = 600) -> str:
    """파일 타입에 따라 텍스트 미리보기 추출"""
    name = (title or "").lower()
    if name.endswith(".pdf"):
        return _preview_pdf_bytes(data, max_pages=3, max_chars=900)
    if _is_office_zip(name):
        return _preview_office_zip(title, data, max_chars=max_chars)
    # 일반 텍스트
    try:
        sample = data[:4096]
        text = sample.decode("utf-8", errors="ignore")
        text = re.sub(r"\s+", " ", text).strip()
        return text[:max_chars] + (" ..." if len(text) > max_chars else "")
    except Exception:
        return f"[binary {len(data)} bytes]"


# ===================== Google Drive Provider =====================

def _get_google_env():
    return os.getenv("GOOGLE_CLIENT_ID", ""), os.getenv("GOOGLE_CLIENT_SECRET", "")


class GoogleProvider:
    name = "google"

    async def refresh_token(self, user_name: str) -> None:
        """토큰 갱신"""
        row = _read_token_row(user_name, self.name)
        rt = (row.get("refresh_token") or _extract_tokens(row).get("refresh_token") or "").strip()
        if not rt:
            LOG.error("[google] refresh_token not found")
            return

        cid, csec = _get_google_env()
        if not cid or not csec:
            raise RuntimeError("Google Client ID/Secret not configured")

        data = {
            "grant_type": "refresh_token",
            "refresh_token": rt,
            "client_id": cid,
            "client_secret": csec
        }

        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post("https://oauth2.googleapis.com/token", data=data)

        if r.status_code != 200:
            LOG.error(f"[google] token refresh failed: {r.text}")
            return

        tok = r.json()
        now = int(time.time())
        new_tokens = {
            "access_token": tok.get("access_token"),
            "refresh_token": tok.get("refresh_token") or rt,
            "scope": tok.get("scope"),
            "expires_at": now + int(tok.get("expires_in", 3600)),
        }
        save_connection(user_name, self.name, new_tokens)

    async def _get_valid_token(self, user_name: str) -> str:
        row = _read_token_row(user_name, self.name)
        tok = _extract_tokens(row)
        if (tok.get("expires_at") or 0) < int(time.time()) + 60:
            await self.refresh_token(user_name)
            row = _read_token_row(user_name, self.name)
            tok = _extract_tokens(row)
        return tok.get("access_token") or ""

    def _extract_id_from_link(self, link: str) -> Optional[str]:
        patterns = [
            r"/folders/([a-zA-Z0-9_-]+)",
            r"/file/d/([a-zA-Z0-9_-]+)",
            r"/document/d/([a-zA-Z0-9_-]+)",
            r"/spreadsheets/d/([a-zA-Z0-9_-]+)",
            r"/presentation/d/([a-zA-Z0-9_-]+)",
            r"id=([a-zA-Z0-9_-]+)"
        ]
        for pat in patterns:
            m = re.search(pat, link)
            if m:
                return m.group(1)
        if re.match(r"^[a-zA-Z0-9_-]{20,}$", link):
            return link
        return None

    async def _get_file_metadata(self, token: str, file_id: str) -> Optional[dict]:
        url = f"https://www.googleapis.com/drive/v3/files/{file_id}"
        headers = {"Authorization": f"Bearer {token}"}
        params = {"fields": "id,name,mimeType,size"}
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(url, headers=headers, params=params)
        return r.json() if r.status_code == 200 else None

    async def _download_file(self, token: str, file_id: str) -> Optional[bytes]:
        url = f"https://www.googleapis.com/drive/v3/files/{file_id}"
        headers = {"Authorization": f"Bearer {token}"}
        params = {"alt": "media"}
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as c:
            r = await c.get(url, headers=headers, params=params)
        return r.content if r.status_code == 200 else None

    async def _export_native_file(self, token: str, file_id: str, mime: str) -> Optional[bytes]:
        """Google 네이티브 파일 export"""
        export_map = {
            "application/vnd.google-apps.document": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.google-apps.spreadsheet": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.google-apps.presentation": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        }
        target = export_map.get(mime, "application/pdf")
        url = f"https://www.googleapis.com/drive/v3/files/{file_id}/export"
        headers = {"Authorization": f"Bearer {token}"}
        params = {"mimeType": target}
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as c:
            r = await c.get(url, headers=headers, params=params)
        return r.content if r.status_code == 200 else None

    async def _list_folder(self, token: str, folder_id: str, limit: int = 50) -> List[Dict]:
        """폴더 내 파일 목록"""
        query = f"'{folder_id}' in parents and trashed = false"
        url = "https://www.googleapis.com/drive/v3/files"
        headers = {"Authorization": f"Bearer {token}"}
        params = {"q": query, "pageSize": limit, "fields": "files(id,name,mimeType,size)"}

        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(url, headers=headers, params=params)

        if r.status_code != 200:
            return []

        return r.json().get("files", [])

    async def enumerate_from_link(self, user_name: str, link: str, max_files: int = 30) -> List[Dict[str, str]]:
        """링크에서 문서 목록 추출"""
        token = await self._get_valid_token(user_name)
        if not token:
            return []

        item_id = self._extract_id_from_link(link)
        if not item_id:
            return []

        meta = await self._get_file_metadata(token, item_id)
        if not meta:
            return []

        docs = []
        is_folder = "application/vnd.google-apps.folder" in meta.get("mimeType", "")

        if is_folder:
            files = await self._list_folder(token, item_id, limit=max_files)
            for f in files:
                name = f.get("name", "")
                mt = f.get("mimeType", "")
                size = int(f.get("size") or 0)
                preview = ""

                try:
                    if mt.startswith("application/vnd.google-apps."):
                        data = await self._export_native_file(token, f["id"], mt)
                        if data:
                            ext = ".docx" if "document" in mt else ".xlsx" if "spreadsheet" in mt else ".pptx"
                            preview = text_preview(name + ext, data)
                    elif size <= MAX_PREVIEW_BYTES:
                        data = await self._download_file(token, f["id"])
                        if data:
                            preview = text_preview(name, data)
                except Exception as e:
                    LOG.debug(f"[google preview] {e}")

                docs.append({
                    "id": f["id"],
                    "title": name,
                    "mimeType": mt,
                    "content": preview or f"[유형: {mt}]"
                })
        else:
            # 단일 파일
            name = meta.get("name", "")
            mt = meta.get("mimeType", "")
            size = int(meta.get("size") or 0)
            preview = ""

            try:
                if mt.startswith("application/vnd.google-apps."):
                    data = await self._export_native_file(token, item_id, mt)
                    if data:
                        ext = ".docx" if "document" in mt else ".xlsx" if "spreadsheet" in mt else ".pptx"
                        preview = text_preview(name + ext, data)
                elif size <= MAX_PREVIEW_BYTES:
                    data = await self._download_file(token, item_id)
                    if data:
                        preview = text_preview(name, data)
            except Exception as e:
                LOG.debug(f"[google preview single] {e}")

            docs.append({
                "id": item_id,
                "title": name,
                "mimeType": mt,
                "content": preview or f"[유형: {mt}]"
            })

        return docs


# ===================== OneDrive Provider =====================

ONEDRIVE_TENANT = os.getenv("ONEDRIVE_TENANT", "common")
ONEDRIVE_TOKEN_URL = f"https://login.microsoftonline.com/{ONEDRIVE_TENANT}/oauth2/v2.0/token"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"


class OneDriveProvider:
    name = "onedrive"

    async def refresh_token(self, user_name: str) -> None:
        row = _read_token_row(user_name, self.name)
        tok = _extract_tokens(row)
        rt = (tok.get("refresh_token") or "").strip()
        if not rt:
            raise RuntimeError("onedrive refresh_token not found")

        cid = os.getenv("ONEDRIVE_CLIENT_ID", "")
        csec = os.getenv("ONEDRIVE_CLIENT_SECRET", "")
        if not cid or not csec:
            raise RuntimeError("ONEDRIVE_CLIENT_ID/SECRET not configured")

        data = {
            "client_id": cid,
            "client_secret": csec,
            "grant_type": "refresh_token",
            "refresh_token": rt,
        }

        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(ONEDRIVE_TOKEN_URL, data=data)

        if r.status_code != 200:
            raise RuntimeError(f"OneDrive token refresh failed: {r.text}")

        js = r.json()
        now = int(time.time())
        new_tokens = {
            "access_token": js.get("access_token"),
            "refresh_token": js.get("refresh_token") or rt,
            "scope": js.get("scope"),
            "expires_at": now + int(js.get("expires_in", 3600)),
        }
        save_connection(user_name, self.name, new_tokens)

    async def _get_valid_token(self, user_name: str) -> str:
        row = _read_token_row(user_name, self.name)
        tok = _extract_tokens(row)
        if (tok.get("expires_at") or 0) < int(time.time()) + 60:
            await self.refresh_token(user_name)
            row = _read_token_row(user_name, self.name)
            tok = _extract_tokens(row)
        return tok.get("access_token") or ""

    def _to_share_id(self, link: str) -> Optional[str]:
        try:
            b64 = base64.urlsafe_b64encode(link.encode("utf-8")).decode("ascii").rstrip("=")
            return "u!" + b64
        except Exception:
            return None

    async def list_my_drive(self, user_name: str, folder_id: str = "root", limit: int = 50) -> List[Dict]:
        """내 드라이브 파일 목록"""
        token = await self._get_valid_token(user_name)
        headers = {"Authorization": f"Bearer {token}"}

        if folder_id == "root":
            url = f"{GRAPH_BASE}/me/drive/root/children"
        else:
            url = f"{GRAPH_BASE}/me/drive/items/{folder_id}/children"

        params = {"$top": limit}

        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(url, headers=headers, params=params)

        if r.status_code == 401:
            await self.refresh_token(user_name)
            token = await self._get_valid_token(user_name)
            headers["Authorization"] = f"Bearer {token}"
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.get(url, headers=headers, params=params)

        if r.status_code != 200:
            return []

        items = r.json().get("value", [])
        docs = []
        for it in items[:limit]:
            name = it.get("name", "")
            if "folder" in it:
                docs.append({"id": it["id"], "title": f"[폴더] {name}", "content": ""})
            else:
                docs.append({"id": it["id"], "title": name, "content": "[OneDrive item]"})
        return docs

    async def enumerate_from_link(self, user_name: str, link: str, max_files: int = 50) -> List[Dict[str, str]]:
        """공유 링크에서 문서 목록 추출"""
        docs = []

        # 1) 공개 shares API 시도
        share_id = self._to_share_id(link)
        if share_id:
            try:
                url = f"https://api.onedrive.com/v1.0/shares/{share_id}/driveItem?expand=children"
                async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
                    r = await c.get(url)

                if r.status_code == 200:
                    item = r.json()
                    children = item.get("children") or item.get("value") or []

                    for ch in children[:max_files]:
                        name = ch.get("name", "")
                        if "folder" in ch:
                            docs.append({"id": ch["id"], "title": f"[폴더] {name}", "content": ""})
                        else:
                            dl = ch.get("@content.downloadUrl") or ch.get("@microsoft.graph.downloadUrl") or ""
                            preview = ""
                            if dl:
                                try:
                                    data = await _http_get_bytes(dl, timeout=60)
                                    if data:
                                        preview = text_preview(name, data)
                                except Exception:
                                    pass
                            docs.append({"id": ch["id"], "title": name, "content": preview or "[OneDrive file]"})

                    if docs:
                        return docs
            except Exception as e:
                LOG.debug(f"[onedrive shares] {e}")

        # 2) Graph API로 내 드라이브 폴백
        try:
            docs = await self.list_my_drive(user_name, folder_id="root", limit=max_files)
        except Exception as e:
            LOG.debug(f"[onedrive mydrive] {e}")

        return docs


# ===================== Notion Provider =====================

NOTION_API_SEARCH = "https://api.notion.com/v1/search"
NOTION_VERSION = "2022-06-28"


class NotionProvider:
    name = "notion"

    async def _get_token(self, user_name: str) -> str:
        row = _read_token_row(user_name, self.name)
        return row.get("access_token") or _extract_tokens(row).get("access_token") or ""

    async def search_pages(self, user_name: str, query: str = "", limit: int = 30) -> List[Dict]:
        """Notion 페이지 검색"""
        token = await self._get_token(user_name)
        if not token:
            return []

        headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }

        payload = {
            "query": query or "",
            "page_size": limit,
            "sort": {"direction": "descending", "timestamp": "last_edited_time"},
        }

        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(NOTION_API_SEARCH, headers=headers, json=payload)

        if r.status_code != 200:
            return []

        data = r.json()
        docs = []

        for item in data.get("results", []):
            url = item.get("url", "")
            last_edited = item.get("last_edited_time", "")
            title = "제목 없음"

            try:
                if item.get("object") == "page":
                    props = item.get("properties") or {}
                    for _, v in props.items():
                        if v and v.get("type") == "title":
                            t = v.get("title") or []
                            if t and t[0].get("plain_text"):
                                title = t[0]["plain_text"]
                                break
                elif item.get("object") == "database":
                    tt = item.get("title") or []
                    if tt and tt[0].get("plain_text"):
                        title = tt[0]["plain_text"]
            except Exception:
                pass

            docs.append({
                "id": item.get("id", ""),
                "title": title,
                "url": url,
                "last_edited_time": last_edited,
                "content": f"[Notion {item.get('object', 'page')}]"
            })

        return docs

    async def enumerate_from_link(self, user_name: str, link: str = "", max_files: int = 30) -> List[Dict]:
        """Notion 문서 목록 (검색 기반)"""
        return await self.search_pages(user_name, query="", limit=max_files)


# ===================== Provider Registry =====================

REGISTRY = {
    "google": GoogleProvider(),
    "onedrive": OneDriveProvider(),
    "notion": NotionProvider(),
}


async def get_documents_from_provider(user_name: str, provider: str, link: str = "", max_files: int = 30) -> List[Dict]:
    """프로바이더에서 문서 목록 가져오기"""
    p = REGISTRY.get(provider.lower())
    if not p:
        return []
    return await p.enumerate_from_link(user_name, link, max_files)
