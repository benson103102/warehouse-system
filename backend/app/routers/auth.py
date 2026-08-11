"""帳號登入端點（註冊／登入／查目前帳號／登出）。

密碼以 PBKDF2 雜湊存放（見 security.py），絕不明文儲存。登入成功回一組 token，
前端存起來、之後每次請求帶 Authorization: Bearer <token>。正式部署時前面會有 HTTPS
（Render 自動提供），密碼與 token 在傳輸過程都受 TLS 保護。
"""

import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator

from .. import security, store
from ..deps import get_current_user

router = APIRouter(prefix="/api/auth", tags=["auth"])

# 基本 email 格式檢查。這裡不做寄信驗證，用簡單 regex 即可，避免多引入 email-validator 套件。
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _check_email(v: str) -> str:
    v = v.strip()
    if not _EMAIL_RE.match(v):
        raise ValueError("email 格式不正確")
    return v


class LoginBody(BaseModel):
    """登入：只檢查 email 格式與密碼非空。不套用註冊的密碼長度規則——登入該做的是
    「比對憑證」，密碼對不對一律回 401，而不是因為長度先擋成 422（那會洩漏規則、也不合理）。"""
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def _email_format(cls, v: str) -> str:
        return _check_email(v)

    @field_validator("password")
    @classmethod
    def _password_nonempty(cls, v: str) -> str:
        if not v:
            raise ValueError("密碼不可為空")
        return v


class RegisterBody(BaseModel):
    """註冊：額外要求密碼至少 6 個字元。"""
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def _email_format(cls, v: str) -> str:
        return _check_email(v)

    @field_validator("password")
    @classmethod
    def _password_len(cls, v: str) -> str:
        if len(v) < 6:
            raise ValueError("密碼至少 6 個字元")
        return v


def _issue_token(user_id: int, email: str) -> dict:
    token = security.new_token()
    store.create_token(token, user_id)
    return {"token": token, "email": email, "user_id": user_id}


@router.post("/register")
def register(body: RegisterBody):
    email = body.email.lower()
    password_hash = security.hash_password(body.password)
    try:
        user_id = store.create_user(email, password_hash)
    except store.EmailTakenError:
        raise HTTPException(status_code=409, detail="這個 email 已經註冊過了，請直接登入")
    # 註冊完直接發 token 當作自動登入，前端不必再打一次 /login。
    return _issue_token(user_id, email)


@router.post("/login")
def login(body: LoginBody):
    user = store.get_user_by_email(body.email.lower())
    # 不論帳號不存在或密碼錯誤，都回同一句話，避免洩漏「這個 email 有沒有註冊過」。
    if user is None or not security.verify_password(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="email 或密碼錯誤")
    return _issue_token(user["id"], user["email"])


@router.get("/me")
def me(user: dict = Depends(get_current_user)):
    row = store.get_user_by_id(user["id"])
    if row is None:
        raise HTTPException(status_code=401, detail="帳號不存在")
    return {"user_id": row["id"], "email": row["email"], "created_at": row["created_at"]}


class LogoutBody(BaseModel):
    token: str


@router.post("/logout")
def logout(body: LogoutBody):
    """撤銷指定 token（登出）。冪等：token 不存在也回成功。"""
    store.delete_token(body.token)
    return {"status": "ok"}
