# backend/storage.py
# CloudRAG - 단일 사용자 모델 (SQLite + PostgreSQL 지원)
import os
import json
import time
from typing import Optional, Dict, Any, List
from contextlib import contextmanager

# ===================== 데이터베이스 설정 =====================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MEDIA_ROOT = os.getenv("MEDIA_ROOT", os.path.join(BASE_DIR, "..", "data", "media"))

# DATABASE_URL이 있으면 PostgreSQL, 없으면 SQLite
DATABASE_URL = os.getenv("DATABASE_URL", "")
USE_POSTGRES = DATABASE_URL.startswith("postgres")

if USE_POSTGRES:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    print(f"[DB] Using PostgreSQL")
else:
    import sqlite3
    DB_PATH = os.getenv("DB_PATH", os.path.join(BASE_DIR, "cloudrag.db"))
    os.makedirs(os.path.dirname(DB_PATH) if os.path.dirname(DB_PATH) else ".", exist_ok=True)
    print(f"[DB] Using SQLite: {DB_PATH}")

os.makedirs(MEDIA_ROOT, exist_ok=True)


# ===================== 연결 관리 =====================

@contextmanager
def get_conn():
    """데이터베이스 연결 컨텍스트 매니저"""
    if USE_POSTGRES:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def _placeholder(index: int = None) -> str:
    """SQL placeholder (PostgreSQL: %s, SQLite: ?)"""
    return "%s" if USE_POSTGRES else "?"


def _now_default() -> str:
    """현재 시간 기본값"""
    if USE_POSTGRES:
        return "EXTRACT(EPOCH FROM NOW())::INTEGER"
    return "strftime('%s', 'now')"


# ===================== 데이터베이스 초기화 =====================

def init_db():
    """데이터베이스 초기화"""
    with get_conn() as conn:
        cur = conn.cursor()

        if USE_POSTGRES:
            # PostgreSQL 테이블
            cur.execute("""
                CREATE TABLE IF NOT EXISTS "user" (
                    id SERIAL PRIMARY KEY,
                    user_name TEXT UNIQUE NOT NULL,
                    password TEXT NOT NULL,
                    display_name TEXT,
                    email TEXT UNIQUE,
                    phone TEXT UNIQUE,
                    created_at INTEGER DEFAULT EXTRACT(EPOCH FROM NOW())::INTEGER,
                    updated_at INTEGER DEFAULT EXTRACT(EPOCH FROM NOW())::INTEGER
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_connections (
                    id SERIAL PRIMARY KEY,
                    user_name TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    access_token TEXT,
                    refresh_token TEXT,
                    token_type TEXT DEFAULT 'Bearer',
                    scope TEXT,
                    expires_at INTEGER,
                    meta_json TEXT,
                    provider_account_id TEXT,
                    provider_account_email TEXT,
                    created_at INTEGER DEFAULT EXTRACT(EPOCH FROM NOW())::INTEGER,
                    updated_at INTEGER DEFAULT EXTRACT(EPOCH FROM NOW())::INTEGER,
                    UNIQUE(user_name, provider)
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS chat_history (
                    id SERIAL PRIMARY KEY,
                    user_name TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    provider TEXT,
                    created_at INTEGER DEFAULT EXTRACT(EPOCH FROM NOW())::INTEGER
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS document_cache (
                    id SERIAL PRIMARY KEY,
                    user_name TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    doc_id TEXT NOT NULL,
                    doc_title TEXT,
                    content_preview TEXT,
                    summary TEXT,
                    created_at INTEGER DEFAULT EXTRACT(EPOCH FROM NOW())::INTEGER,
                    updated_at INTEGER DEFAULT EXTRACT(EPOCH FROM NOW())::INTEGER,
                    UNIQUE(user_name, provider, doc_id)
                )
            """)
        else:
            # SQLite 테이블
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_name TEXT UNIQUE NOT NULL,
                    password TEXT NOT NULL,
                    display_name TEXT,
                    email TEXT UNIQUE,
                    phone TEXT UNIQUE,
                    created_at INTEGER DEFAULT (strftime('%s', 'now')),
                    updated_at INTEGER DEFAULT (strftime('%s', 'now'))
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_connections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_name TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    access_token TEXT,
                    refresh_token TEXT,
                    token_type TEXT DEFAULT 'Bearer',
                    scope TEXT,
                    expires_at INTEGER,
                    meta_json TEXT,
                    provider_account_id TEXT,
                    provider_account_email TEXT,
                    created_at INTEGER DEFAULT (strftime('%s', 'now')),
                    updated_at INTEGER DEFAULT (strftime('%s', 'now')),
                    UNIQUE(user_name, provider)
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS chat_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_name TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    provider TEXT,
                    created_at INTEGER DEFAULT (strftime('%s', 'now'))
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS document_cache (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_name TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    doc_id TEXT NOT NULL,
                    doc_title TEXT,
                    content_preview TEXT,
                    summary TEXT,
                    created_at INTEGER DEFAULT (strftime('%s', 'now')),
                    updated_at INTEGER DEFAULT (strftime('%s', 'now')),
                    UNIQUE(user_name, provider, doc_id)
                )
            """)

    db_type = "PostgreSQL" if USE_POSTGRES else "SQLite"
    print(f"[DB] {db_type} initialized successfully")


# ===================== 사용자 관리 =====================

def create_user(user_name: str, password: str) -> bool:
    """사용자 생성"""
    ph = _placeholder()
    with get_conn() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                f'INSERT INTO "user" (user_name, password) VALUES ({ph}, {ph})',
                (user_name.strip(), password)
            )
            return True
        except Exception as e:
            if "unique" in str(e).lower() or "duplicate" in str(e).lower():
                raise ValueError("USERNAME_TAKEN")
            raise


def verify_login(user_name: str, password: str) -> bool:
    """로그인 검증"""
    ph = _placeholder()
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f'SELECT password FROM "user" WHERE user_name = {ph}',
            (user_name.strip(),)
        )
        row = cur.fetchone()
        if row:
            stored_pw = row["password"] if USE_POSTGRES else row["password"]
            return stored_pw == password
    return False


def get_user(user_name: str) -> Optional[Dict]:
    """사용자 정보 조회"""
    ph = _placeholder()
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f'SELECT * FROM "user" WHERE user_name = {ph}',
            (user_name.strip(),)
        )
        row = cur.fetchone()
        return dict(row) if row else None


def update_user_profile(user_name: str, display_name: str = None,
                        email: str = None, phone: str = None) -> bool:
    """사용자 프로필 업데이트"""
    updates = []
    params = []
    ph = _placeholder()

    if display_name is not None:
        updates.append(f"display_name = {ph}")
        params.append(display_name)
    if email is not None:
        updates.append(f"email = {ph}")
        params.append(email)
    if phone is not None:
        updates.append(f"phone = {ph}")
        params.append(phone)

    if not updates:
        return False

    updates.append(f"updated_at = {ph}")
    params.append(int(time.time()))
    params.append(user_name.strip())

    with get_conn() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                f'UPDATE "user" SET {", ".join(updates)} WHERE user_name = {ph}',
                params
            )
            return True
        except Exception as e:
            err = str(e).lower()
            if "email" in err:
                raise ValueError("EMAIL_IN_USE")
            if "phone" in err:
                raise ValueError("PHONE_IN_USE")
            raise


def is_unique_user_name(user_name: str) -> bool:
    """사용자명 중복 확인"""
    ph = _placeholder()
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f'SELECT 1 FROM "user" WHERE lower(user_name) = lower({ph})',
            (user_name.strip(),)
        )
        return cur.fetchone() is None


def is_unique_email(email: str) -> bool:
    """이메일 중복 확인"""
    ph = _placeholder()
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f'SELECT 1 FROM "user" WHERE email IS NOT NULL AND lower(email) = lower({ph})',
            (email.strip(),)
        )
        return cur.fetchone() is None


# ===================== 연결 관리 (OAuth) =====================

def save_connection(user_name: str, provider: str, token: Dict,
                    provider_account_id: str = None,
                    provider_account_email: str = None,
                    meta: Dict = None) -> bool:
    """OAuth 연결 저장"""
    ph = _placeholder()
    now = int(time.time())
    meta_json = json.dumps(meta or {}, ensure_ascii=False)

    with get_conn() as conn:
        cur = conn.cursor()

        if USE_POSTGRES:
            cur.execute(f"""
                INSERT INTO user_connections
                    (user_name, provider, access_token, refresh_token, token_type,
                     scope, expires_at, meta_json, provider_account_id, provider_account_email,
                     created_at, updated_at)
                VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})
                ON CONFLICT(user_name, provider) DO UPDATE SET
                    access_token = EXCLUDED.access_token,
                    refresh_token = COALESCE(EXCLUDED.refresh_token, user_connections.refresh_token),
                    token_type = EXCLUDED.token_type,
                    scope = EXCLUDED.scope,
                    expires_at = EXCLUDED.expires_at,
                    meta_json = EXCLUDED.meta_json,
                    provider_account_id = EXCLUDED.provider_account_id,
                    provider_account_email = EXCLUDED.provider_account_email,
                    updated_at = EXCLUDED.updated_at
            """, (
                user_name.strip(), provider.lower(),
                token.get("access_token"), token.get("refresh_token"),
                token.get("token_type", "Bearer"), token.get("scope"),
                token.get("expires_at"), meta_json,
                provider_account_id, provider_account_email,
                now, now
            ))
        else:
            cur.execute(f"""
                INSERT INTO user_connections
                    (user_name, provider, access_token, refresh_token, token_type,
                     scope, expires_at, meta_json, provider_account_id, provider_account_email,
                     created_at, updated_at)
                VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})
                ON CONFLICT(user_name, provider) DO UPDATE SET
                    access_token = excluded.access_token,
                    refresh_token = COALESCE(excluded.refresh_token, user_connections.refresh_token),
                    token_type = excluded.token_type,
                    scope = excluded.scope,
                    expires_at = excluded.expires_at,
                    meta_json = excluded.meta_json,
                    provider_account_id = excluded.provider_account_id,
                    provider_account_email = excluded.provider_account_email,
                    updated_at = excluded.updated_at
            """, (
                user_name.strip(), provider.lower(),
                token.get("access_token"), token.get("refresh_token"),
                token.get("token_type", "Bearer"), token.get("scope"),
                token.get("expires_at"), meta_json,
                provider_account_id, provider_account_email,
                now, now
            ))
    return True


def get_connection_row(user_name: str, provider: str) -> Optional[Dict]:
    """연결 정보 조회"""
    ph = _placeholder()
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT * FROM user_connections WHERE user_name = {ph} AND provider = {ph}",
            (user_name.strip(), provider.lower())
        )
        row = cur.fetchone()
        return dict(row) if row else None


def get_provider_token(user_name: str, provider: str) -> Optional[Dict]:
    """프로바이더 토큰 조회 (get_connection_row 별칭)"""
    return get_connection_row(user_name, provider)


def get_all_connections(user_name: str) -> List[Dict]:
    """사용자의 모든 연결 목록"""
    ph = _placeholder()
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT provider, provider_account_email FROM user_connections WHERE user_name = {ph}",
            (user_name.strip(),)
        )
        rows = cur.fetchall()
        return [dict(r) for r in rows]


def delete_connection(user_name: str, provider: str) -> bool:
    """연결 삭제"""
    ph = _placeholder()
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f"DELETE FROM user_connections WHERE user_name = {ph} AND provider = {ph}",
            (user_name.strip(), provider.lower())
        )
    return True


def get_connection_meta(user_name: str, provider: str) -> Optional[Dict]:
    """연결 메타데이터 조회"""
    row = get_connection_row(user_name, provider)
    if not row:
        return None
    try:
        return json.loads(row.get("meta_json") or "{}")
    except:
        return {}


def update_connection_meta(user_name: str, provider: str, meta_updates: Dict) -> bool:
    """연결 메타데이터 업데이트"""
    ph = _placeholder()
    current = get_connection_meta(user_name, provider) or {}
    current.update(meta_updates)

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE user_connections SET meta_json = {ph}, updated_at = {ph} WHERE user_name = {ph} AND provider = {ph}",
            (json.dumps(current, ensure_ascii=False), int(time.time()), user_name.strip(), provider.lower())
        )
    return True


def get_notion_token(user_name: str) -> Optional[Dict]:
    """Notion 토큰 조회 (하위 호환)"""
    return get_connection_row(user_name, "notion")


# ===================== 채팅 히스토리 =====================

def add_chat_history(user_name: str, role: str, content: str, provider: str = None):
    """채팅 기록 추가"""
    ph = _placeholder()
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f"INSERT INTO chat_history (user_name, role, content, provider) VALUES ({ph}, {ph}, {ph}, {ph})",
            (user_name.strip(), role, content, provider)
        )


def get_chat_history(user_name: str, limit: int = 20) -> List[Dict]:
    """채팅 기록 조회"""
    ph = _placeholder()
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT role, content, provider, created_at FROM chat_history WHERE user_name = {ph} ORDER BY id DESC LIMIT {ph}",
            (user_name.strip(), limit)
        )
        rows = cur.fetchall()
        return [dict(r) for r in reversed(rows)]


def clear_chat_history(user_name: str):
    """채팅 기록 삭제"""
    ph = _placeholder()
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f"DELETE FROM chat_history WHERE user_name = {ph}",
            (user_name.strip(),)
        )


# ===================== 문서 캐시 =====================

def cache_document(user_name: str, provider: str, doc_id: str,
                   doc_title: str = None, content_preview: str = None,
                   summary: str = None):
    """문서 캐시 저장"""
    ph = _placeholder()
    now = int(time.time())

    with get_conn() as conn:
        cur = conn.cursor()

        if USE_POSTGRES:
            cur.execute(f"""
                INSERT INTO document_cache (user_name, provider, doc_id, doc_title, content_preview, summary, created_at, updated_at)
                VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})
                ON CONFLICT(user_name, provider, doc_id) DO UPDATE SET
                    doc_title = COALESCE(EXCLUDED.doc_title, document_cache.doc_title),
                    content_preview = COALESCE(EXCLUDED.content_preview, document_cache.content_preview),
                    summary = COALESCE(EXCLUDED.summary, document_cache.summary),
                    updated_at = EXCLUDED.updated_at
            """, (user_name.strip(), provider.lower(), doc_id, doc_title, content_preview, summary, now, now))
        else:
            cur.execute(f"""
                INSERT INTO document_cache (user_name, provider, doc_id, doc_title, content_preview, summary, created_at, updated_at)
                VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})
                ON CONFLICT(user_name, provider, doc_id) DO UPDATE SET
                    doc_title = COALESCE(excluded.doc_title, document_cache.doc_title),
                    content_preview = COALESCE(excluded.content_preview, document_cache.content_preview),
                    summary = COALESCE(excluded.summary, document_cache.summary),
                    updated_at = excluded.updated_at
            """, (user_name.strip(), provider.lower(), doc_id, doc_title, content_preview, summary, now, now))


def get_cached_document(user_name: str, provider: str, doc_id: str) -> Optional[Dict]:
    """캐시된 문서 조회"""
    ph = _placeholder()
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT * FROM document_cache WHERE user_name = {ph} AND provider = {ph} AND doc_id = {ph}",
            (user_name.strip(), provider.lower(), doc_id)
        )
        row = cur.fetchone()
        return dict(row) if row else None


def get_cached_documents(user_name: str, provider: str = None, limit: int = 50) -> List[Dict]:
    """캐시된 문서 목록"""
    ph = _placeholder()
    with get_conn() as conn:
        cur = conn.cursor()
        if provider:
            cur.execute(
                f"SELECT * FROM document_cache WHERE user_name = {ph} AND provider = {ph} ORDER BY updated_at DESC LIMIT {ph}",
                (user_name.strip(), provider.lower(), limit)
            )
        else:
            cur.execute(
                f"SELECT * FROM document_cache WHERE user_name = {ph} ORDER BY updated_at DESC LIMIT {ph}",
                (user_name.strip(), limit)
            )
        rows = cur.fetchall()
        return [dict(r) for r in rows]


# 하위 호환을 위한 DB_PATH export
if not USE_POSTGRES:
    DB_PATH = DB_PATH
else:
    DB_PATH = DATABASE_URL

# 초기화 실행
init_db()
