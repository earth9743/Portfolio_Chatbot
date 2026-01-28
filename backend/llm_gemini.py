# backend/llm_gemini.py
# CloudRAG - Gemini LLM 연동
# -*- coding: utf-8 -*-
"""
Gemini 호출 유틸
 - answer_with_gemini(query, chunks): RAG 기반 Q&A
 - summarize_text(text): 텍스트 요약
 - summarize_documents(docs): 문서 목록 요약
"""

import os
import re
from typing import Iterable, List, Tuple, Optional, Union

from dotenv import load_dotenv
import google.generativeai as genai

# -----------------------------
# 설정 및 모델 핸들링
# -----------------------------
load_dotenv()
API_KEY = os.getenv("GEMINI_API_KEY")
if not API_KEY:
    raise RuntimeError("GEMINI_API_KEY가 설정되지 않았습니다. .env 파일을 확인하세요.")

DEFAULT_MODEL = "gemini-2.5-flash-preview-04-17"
FALLBACK_MODEL = "gemini-1.5-flash"


def _get_model(model_name: str = None):
    """Gemini 모델 인스턴스 반환"""
    genai.configure(api_key=API_KEY)
    try:
        return genai.GenerativeModel(model_name or DEFAULT_MODEL)
    except Exception:
        return genai.GenerativeModel(FALLBACK_MODEL)


# -----------------------------
# RAG 기반 Q&A
# -----------------------------
async def answer_with_gemini(query: str, chunks: Optional[Iterable[Tuple[str, str]]] = None) -> str:
    """
    문서 청크를 참고하여 질문에 답변

    Args:
        query: 사용자 질문
        chunks: [(title, content), ...] 형태의 참고 문서 목록

    Returns:
        Gemini의 답변 텍스트
    """
    model = _get_model()

    sys_prompt = (
        "당신은 'CloudRAG'의 AI 어시스턴트입니다. "
        "사용자가 연동한 클라우드 드라이브(Google Drive, OneDrive, Notion)의 문서를 기반으로 질문에 답변합니다.\n\n"
        "규칙:\n"
        "1. 답변은 반드시 제공된 '참고 문서'만을 근거로 해야 합니다.\n"
        "2. 질문에 대한 답변을 참고 문서에서 찾을 수 없는 경우, '연동된 문서에서 해당 정보를 찾을 수 없습니다.'라고 답변하세요.\n"
        "3. 질문 의도를 파악해 관련 내용을 최대한 찾아 종합·요약하세요.\n"
        "4. 답변은 명확하고 간결하게 작성하세요.\n"
        "5. 참고한 문서의 제목을 답변 마지막에 출처로 표시하세요."
    )

    parts: List[Union[str, dict]] = [sys_prompt]

    if chunks:
        combined = []
        for i, (title, content) in enumerate(chunks, start=1):
            combined.append(f"--- 참고 문서 {i}: {title} ---\n{content}")
        parts.append("\n" + "\n\n".join(combined))

    parts.append(f"\n--- 사용자 질문 ---\n{query}")
    parts.append("\n--- 답변 ---")

    try:
        resp = model.generate_content(parts)
        return (getattr(resp, "text", None) or "").strip() or "[빈 응답]"
    except Exception as e:
        return f"[Gemini 호출 실패] {e}"


# -----------------------------
# 텍스트 요약
# -----------------------------
async def summarize_text(text: str, max_chars: int = 15000) -> str:
    """
    텍스트 요약

    Args:
        text: 요약할 텍스트
        max_chars: 최대 입력 문자 수

    Returns:
        요약된 텍스트
    """
    if not text or len(text.strip()) < 50:
        return "[요약할 내용이 부족합니다]"

    model = _get_model()

    prompt = (
        "아래 문서를 한국어로 요약해 주세요.\n"
        "- 핵심 내용을 3~5개의 bullet point로 정리\n"
        "- 중요한 수치나 날짜가 있으면 포함\n"
        "- 전문 용어는 그대로 유지\n\n"
        f"[문서 내용]\n{text[:max_chars]}"
    )

    try:
        resp = model.generate_content(prompt)
        return (getattr(resp, "text", None) or "").strip() or "[요약 생성 실패]"
    except Exception as e:
        return f"[요약 실패] {e}"


# -----------------------------
# 다중 문서 요약
# -----------------------------
async def summarize_documents(docs: List[dict], max_docs: int = 10) -> str:
    """
    여러 문서를 종합 요약

    Args:
        docs: [{"title": str, "content": str}, ...] 형태의 문서 목록
        max_docs: 최대 처리 문서 수

    Returns:
        종합 요약 텍스트
    """
    if not docs:
        return "[요약할 문서가 없습니다]"

    model = _get_model()

    # 문서 내용 결합
    combined = []
    for i, doc in enumerate(docs[:max_docs], start=1):
        title = doc.get("title", f"문서 {i}")
        content = doc.get("content", "")[:3000]  # 각 문서당 최대 3000자
        combined.append(f"[문서 {i}: {title}]\n{content}")

    prompt = (
        "아래 여러 문서들을 종합하여 한국어로 요약해 주세요.\n"
        "- 각 문서의 핵심 내용을 파악\n"
        "- 문서들 간의 공통점이나 관련성이 있으면 언급\n"
        "- 전체적인 맥락을 정리\n\n"
        + "\n\n".join(combined)
    )

    try:
        resp = model.generate_content(prompt)
        return (getattr(resp, "text", None) or "").strip() or "[요약 생성 실패]"
    except Exception as e:
        return f"[종합 요약 실패] {e}"


# -----------------------------
# 유틸리티 함수
# -----------------------------
_ZERO_WIDTH = r"[\u200B-\u200D\uFEFF]"
_CTRL = r"[\x00-\x08\x0B\x0C\x0E-\x1F]"


def sanitize_text(s: str) -> str:
    """텍스트 정제 (제어 문자 제거)"""
    if not s:
        return ""
    s = re.sub(_ZERO_WIDTH, "", s)
    s = re.sub(_CTRL, " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def chunk_text(s: str, max_chars: int = 15000) -> List[str]:
    """긴 텍스트를 청크로 분할"""
    s = s or ""
    if len(s) <= max_chars:
        return [s]

    chunks: List[str] = []
    i = 0
    n = len(s)

    while i < n:
        j = min(n, i + max_chars)
        # 문장 단위로 자르기 시도
        cut_pos = max(
            s.rfind("\n\n", i, j),
            s.rfind(". ", i, j),
            s.rfind(" ", i, j)
        )
        if cut_pos == -1 or cut_pos <= i + 2000:
            cut_pos = j
        chunks.append(s[i:cut_pos])
        i = cut_pos

    return [c.strip() for c in chunks if c.strip()]


def validate_context(text: str, min_chars: int = 50) -> Tuple[bool, str]:
    """컨텍스트 유효성 검사"""
    if not text:
        return False, "본문이 비어있음"
    if len(text) < min_chars:
        return False, f"본문이 너무 짧음 ({len(text)}자)"
    if "[binary" in text.lower():
        return False, "바이너리 파일"
    return True, ""
