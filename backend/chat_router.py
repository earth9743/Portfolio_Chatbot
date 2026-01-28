# backend/chat_router.py
# CloudRAG - RAG 기반 챗봇 라우터
# -*- coding: utf-8 -*-

from fastapi import APIRouter, HTTPException, Query, Form
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional, List
import logging

from storage import (
    get_connection_row,
    add_chat_history,
    get_chat_history,
    clear_chat_history,
    cache_document,
    get_cached_documents,
)
from providers import get_documents_from_provider, REGISTRY
from llm_gemini import answer_with_gemini, summarize_text, summarize_documents

LOG = logging.getLogger("uvicorn.error")

router = APIRouter(prefix="/chat", tags=["chat"])


# ===================== Request/Response Models =====================

class AskRequest(BaseModel):
    user_name: str
    question: str
    provider: Optional[str] = None  # google, onedrive, notion, 또는 auto
    link: Optional[str] = ""  # 특정 폴더/파일 링크
    max_docs: Optional[int] = 10


class SummarizeRequest(BaseModel):
    user_name: str
    provider: str
    link: Optional[str] = ""
    max_docs: Optional[int] = 10


# ===================== Chat Endpoints =====================

@router.post("/ask")
async def chat_ask(req: AskRequest):
    """
    RAG 기반 Q&A
    - 연동된 드라이브에서 문서를 가져와 Gemini로 답변 생성
    """
    user_name = req.user_name.strip()
    question = req.question.strip()

    if not user_name or not question:
        raise HTTPException(status_code=400, detail="user_name과 question은 필수입니다")

    # 프로바이더 결정
    providers_to_check = []
    if req.provider and req.provider != "auto":
        providers_to_check = [req.provider.lower()]
    else:
        # auto: 연동된 모든 프로바이더 확인
        for p in ["google", "onedrive", "notion"]:
            if get_connection_row(user_name, p):
                providers_to_check.append(p)

    if not providers_to_check:
        return JSONResponse({
            "ok": False,
            "answer": "연동된 클라우드 드라이브가 없습니다. 먼저 Google Drive, OneDrive, 또는 Notion을 연동해주세요.",
            "sources": []
        })

    # 문서 수집
    all_docs = []
    sources_used = []

    for provider in providers_to_check:
        try:
            docs = await get_documents_from_provider(
                user_name, provider, req.link or "", max_files=req.max_docs or 10
            )
            for doc in docs:
                doc["provider"] = provider
                all_docs.append(doc)
                sources_used.append({
                    "provider": provider,
                    "title": doc.get("title", ""),
                    "id": doc.get("id", "")
                })

                # 캐시 저장
                cache_document(
                    user_name, provider, doc.get("id", ""),
                    doc_title=doc.get("title"),
                    content_preview=doc.get("content", "")[:500]
                )
        except Exception as e:
            LOG.warning(f"[chat_ask] {provider} error: {e}")

    if not all_docs:
        return JSONResponse({
            "ok": False,
            "answer": "연동된 드라이브에서 문서를 찾을 수 없습니다. 드라이브에 문서가 있는지 확인하거나 다시 연동해주세요.",
            "sources": []
        })

    # 청크 준비 (title, content)
    chunks = []
    for doc in all_docs:
        title = doc.get("title", "문서")
        content = doc.get("content", "")
        if content and not content.startswith("["):  # 미리보기 실패 메시지 제외
            chunks.append((title, content))

    if not chunks:
        # 미리보기는 실패했지만 문서 목록은 있음
        return JSONResponse({
            "ok": True,
            "answer": f"총 {len(all_docs)}개의 문서를 찾았지만 내용을 읽을 수 없습니다. 지원되는 파일 형식(PDF, DOCX, TXT 등)인지 확인해주세요.",
            "sources": sources_used[:10]
        })

    # Gemini로 답변 생성
    try:
        answer = await answer_with_gemini(question, chunks)
    except Exception as e:
        LOG.error(f"[chat_ask] Gemini error: {e}")
        answer = f"답변 생성 중 오류가 발생했습니다: {e}"

    # 채팅 히스토리 저장
    add_chat_history(user_name, "user", question, provider=req.provider)
    add_chat_history(user_name, "assistant", answer, provider=req.provider)

    return JSONResponse({
        "ok": True,
        "answer": answer,
        "sources": sources_used[:10],
        "doc_count": len(all_docs)
    })


@router.post("/summarize")
async def chat_summarize(req: SummarizeRequest):
    """
    연동된 드라이브 문서들 요약
    """
    user_name = req.user_name.strip()
    provider = req.provider.lower()

    if not user_name:
        raise HTTPException(status_code=400, detail="user_name은 필수입니다")

    if provider not in REGISTRY:
        raise HTTPException(status_code=400, detail=f"지원하지 않는 provider: {provider}")

    if not get_connection_row(user_name, provider):
        return JSONResponse({
            "ok": False,
            "summary": f"{provider}가 연동되어 있지 않습니다."
        })

    # 문서 가져오기
    try:
        docs = await get_documents_from_provider(
            user_name, provider, req.link or "", max_files=req.max_docs or 10
        )
    except Exception as e:
        return JSONResponse({
            "ok": False,
            "summary": f"문서를 가져오는 중 오류: {e}"
        })

    if not docs:
        return JSONResponse({
            "ok": False,
            "summary": "문서를 찾을 수 없습니다."
        })

    # 유효한 문서만 필터링
    valid_docs = [d for d in docs if d.get("content") and not d["content"].startswith("[")]

    if not valid_docs:
        return JSONResponse({
            "ok": True,
            "summary": f"총 {len(docs)}개의 문서를 찾았지만 내용을 읽을 수 없습니다.",
            "doc_count": len(docs)
        })

    # 요약 생성
    try:
        summary = await summarize_documents(valid_docs, max_docs=10)
    except Exception as e:
        summary = f"요약 생성 실패: {e}"

    return JSONResponse({
        "ok": True,
        "summary": summary,
        "doc_count": len(docs),
        "readable_count": len(valid_docs)
    })


@router.get("/history")
async def get_history(user_name: str = Query(...), limit: int = Query(20)):
    """채팅 히스토리 조회"""
    history = get_chat_history(user_name, limit)
    return {"ok": True, "history": history}


@router.post("/clear-history")
async def clear_history(user_name: str = Form(...)):
    """채팅 히스토리 삭제"""
    clear_chat_history(user_name)
    return {"ok": True, "message": "채팅 기록이 삭제되었습니다"}


@router.get("/cached-docs")
async def get_cached_docs(user_name: str = Query(...), provider: str = Query(None), limit: int = Query(50)):
    """캐시된 문서 목록"""
    docs = get_cached_documents(user_name, provider, limit)
    return {"ok": True, "documents": docs}


# ===================== Provider별 단축 엔드포인트 =====================

@router.post("/google/ask")
async def google_ask(
    user_name: str = Form(...),
    question: str = Form(...),
    link: str = Form(""),
    max_docs: int = Form(10)
):
    """Google Drive 기반 Q&A"""
    req = AskRequest(
        user_name=user_name,
        question=question,
        provider="google",
        link=link,
        max_docs=max_docs
    )
    return await chat_ask(req)


@router.post("/onedrive/ask")
async def onedrive_ask(
    user_name: str = Form(...),
    question: str = Form(...),
    link: str = Form(""),
    max_docs: int = Form(10)
):
    """OneDrive 기반 Q&A"""
    req = AskRequest(
        user_name=user_name,
        question=question,
        provider="onedrive",
        link=link,
        max_docs=max_docs
    )
    return await chat_ask(req)


@router.post("/notion/ask")
async def notion_ask(
    user_name: str = Form(...),
    question: str = Form(...),
    max_docs: int = Form(10)
):
    """Notion 기반 Q&A"""
    req = AskRequest(
        user_name=user_name,
        question=question,
        provider="notion",
        link="",
        max_docs=max_docs
    )
    return await chat_ask(req)


@router.post("/auto/ask")
async def auto_ask(
    user_name: str = Form(...),
    question: str = Form(...),
    max_docs: int = Form(10)
):
    """모든 연동 드라이브에서 자동 검색 Q&A"""
    req = AskRequest(
        user_name=user_name,
        question=question,
        provider="auto",
        link="",
        max_docs=max_docs
    )
    return await chat_ask(req)
