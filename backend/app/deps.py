"""共用的 FastAPI 依賴注入（登入驗證、session id、清洗結果存在性檢查）。

改版重點：資料的「歸屬」不再靠前端自帶的 X-Session-Id 字串（誰知道別人的字串就能存取
別人資料），而是綁定登入後的使用者帳號。前端登入取得 token，之後每次請求帶
`Authorization: Bearer <token>`；後端據此查出是哪位使用者，用該使用者 id 當 session id。

因此所有資料端點（ingest／clean／abc…）只要照舊依賴 get_session_id，就自動變成
「需登入且只能存取自己資料」——不必逐一改每支端點。
"""

from typing import Optional

from fastapi import Depends, Header, HTTPException

from . import state, store


def get_current_user(authorization: Optional[str] = Header(default=None)) -> dict:
    """從 Authorization: Bearer <token> 解析出目前登入的使用者；未登入或 token 失效則 401。"""
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="尚未登入，請先呼叫 /api/auth/login 取得 token")
    user_id = store.get_user_id_by_token(token)
    if user_id is None:
        raise HTTPException(status_code=401, detail="登入憑證無效或已過期，請重新登入")
    return {"id": user_id}


def get_session_id(user: dict = Depends(get_current_user)) -> str:
    """資料歸屬 key：以登入使用者 id 命名，確保每位使用者的資料互相隔離。"""
    return f"u{user['id']}"


def require_clean_result(session_id: str = Depends(get_session_id)) -> "state.SessionState":
    """給需要「已完成清洗」才能算的端點（ABC／共同揀取／預測／批次／儲位／KPI）共用。"""
    sess = state.get_session(session_id)
    if sess.cleaning_result is None:
        raise HTTPException(
            status_code=409,
            detail="尚未執行資料清洗，請先呼叫 POST /api/ingest/upload（或 /api/ingest/sample）"
                   "上傳資料，再呼叫 POST /api/clean/run。",
        )
    return sess
