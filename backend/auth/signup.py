# backend/auth/signup.py
# CloudRAG - 회원가입 라우터
from fastapi import APIRouter, HTTPException, Form
from fastapi.responses import RedirectResponse

from storage import create_user, update_user_profile, is_unique_user_name, is_unique_email

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/signup")
async def signup(
    user_name: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    display_name: str = Form(""),
    email: str = Form(""),
):
    """회원가입"""
    user_name = user_name.strip()
    password = password.strip()

    # 유효성 검사
    if len(user_name) < 4:
        raise HTTPException(status_code=400, detail="아이디는 4자 이상이어야 합니다")

    if len(password) < 6:
        raise HTTPException(status_code=400, detail="비밀번호는 6자 이상이어야 합니다")

    if password != confirm_password:
        raise HTTPException(status_code=400, detail="비밀번호가 일치하지 않습니다")

    if not is_unique_user_name(user_name):
        raise HTTPException(status_code=400, detail="이미 사용 중인 아이디입니다")

    if email and not is_unique_email(email):
        raise HTTPException(status_code=400, detail="이미 사용 중인 이메일입니다")

    # 사용자 생성
    try:
        create_user(user_name, password)
        if display_name or email:
            update_user_profile(user_name, display_name=display_name or None, email=email or None)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return RedirectResponse("/login?signup=ok", status_code=303)


@router.get("/check-id")
async def check_id(user_name: str):
    """아이디 중복 확인"""
    available = is_unique_user_name(user_name.strip())
    return {"available": available}


@router.get("/check-email")
async def check_email(email: str):
    """이메일 중복 확인"""
    available = is_unique_email(email.strip())
    return {"available": available}
