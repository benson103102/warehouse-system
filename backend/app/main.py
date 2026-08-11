"""
FastAPI 應用進入點。

原前端是一個 809KB 的單一 HTML 檔，所有運算（資料清洗、ABC分級、共同揀取分析、
需求預測、批次併單模擬、儲位配置、KPI計算）都寫在 <script> 內、在使用者瀏覽器裡
執行。這支後端把上述運算全部移到伺服器端，前端改成單純呼叫這些 API、拿 JSON 回來
畫圖表。詳細的「原函式 → 新端點」對照表見 docs/架構與移植對照.md。

執行方式：
    pip install -r requirements.txt
    uvicorn app.main:app --reload --port 8000

啟動後可到 http://127.0.0.1:8000/docs 看 Swagger 互動式 API 文件、直接在網頁上試打每支 API。
"""

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import store
from .routers import abc, auth, batch, clean, copick, forecast, ingest, kpi, storage

app = FastAPI(
    title="儲位配置與揀貨策略優化系統 API",
    description="把原本全部在前端運算的倉儲分析原型，改寫成前後端分離＋REST API 架構的教學示範。",
    version="0.1.0",
)

# CORS 來源白名單：本機開發預設全開（方便前端直接開 index.html 連本機後端）；
# 正式部署時用環境變數 WAREHOUSE_CORS_ORIGINS 指定你的前端網域，以逗號分隔，例如：
#   WAREHOUSE_CORS_ORIGINS=https://warehouse-frontend.onrender.com
_origins_env = os.environ.get("WAREHOUSE_CORS_ORIGINS", "").strip()
_allow_origins = [o.strip() for o in _origins_env.split(",") if o.strip()] or ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allow_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(ingest.router)
app.include_router(clean.router)
app.include_router(abc.router)
app.include_router(copick.router)
app.include_router(forecast.router)
app.include_router(batch.router)
app.include_router(storage.router)
app.include_router(kpi.router)


@app.on_event("startup")
def _startup():
    # 狀態改為落地保存後，啟動時確保資料表與資料夾就緒，並印出資料存放位置方便查找／備份。
    store.init_db()
    print(f"[warehouse] 資料落地目錄：{store.DATA_DIR}")


@app.get("/api/health", tags=["health"])
def health():
    return {"status": "ok"}


# 把前端 index.html 一起由後端送出（同源部署）：使用者只要開後端網址就看到登入畫面，
# 前後端同一個網域，完全不用處理 CORS。本機開發時你仍可直接用 file:// 開 index.html。
# 這個 mount 一定要放在所有 /api 路由與 /docs 之後，才不會蓋掉它們。
_FRONTEND_DIR = os.environ.get(
    "WAREHOUSE_FRONTEND_DIR",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "frontend")),
)
if os.path.isdir(_FRONTEND_DIR):
    app.mount("/", StaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend")
