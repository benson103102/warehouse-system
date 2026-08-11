"""
資料匯入端點 — 對應原前端 handleFile()／loadSampleData()。

原型用 xlsx.js（.xlsx）+ 手刻的 ZIP/XML 解析器在瀏覽器端解析檔案。後端版本改用
pandas.read_excel／read_csv，程式碼量少了一個數量級，但正式資料集（「訂單資料」
工作表百萬列等級、解壓後約 700MB 單行 XML）用 openpyxl 讀取實測要 3~4 分鐘 CPU
時間，而且是同步、會整個卡住 event loop。所以這裡刻意拆成兩段：

  1. POST /api/ingest/upload：只做「接收檔案＋快速檢查」，立刻回應，把真正的
     DataFrame 解析丟給 BackgroundTasks 在背景執行緒跑（不會卡住其他請求，包含
     下面第 2 點的輪詢請求本身）。
  2. GET  /api/ingest/status：回報目前狀態（idle／processing／ready／error），
     前端改成每隔幾秒呼叫一次，直到 status 變成 ready 或 error 為止。

讀檔本身也比照原清洗腳本 read_excel_robust() 的作法：優先用 calamine（Rust 實作，
不受這份資料超大單行 XML 的影響，速度也快很多），使用者本機沒裝
python-calamine 時自動退回 openpyxl（能動，只是比較慢）。
"""

import io
import json
import os

import pandas as pd
from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile

from .. import jsonsafe, state, store
from ..deps import get_session_id
from ..services.cleaning_core import ORDER_COLUMNS

router = APIRouter(prefix="/api/ingest", tags=["ingest"])

_SAMPLE_PATH = os.path.join(os.path.dirname(__file__), "..", "services", "fixtures", "sample_orders.json")


def _read_excel_sheet(content: bytes, engine: str) -> pd.DataFrame:
    try:
        return pd.read_excel(io.BytesIO(content), sheet_name="訂單資料", dtype=object, engine=engine)
    except ValueError as e:
        # 找不到「訂單資料」工作表時，退而使用第一個工作表；其餘 ValueError（例如
        # 檔案本身不是合法 xlsx）原樣往上丟。
        if "Worksheet" in str(e) or "訂單資料" in str(e):
            return pd.read_excel(io.BytesIO(content), sheet_name=0, dtype=object, engine=engine)
        raise


def _read_excel_robust(content: bytes) -> pd.DataFrame:
    """優先 calamine、失敗或未安裝才退回 openpyxl。對應原清洗腳本的 read_excel_robust()。"""
    try:
        return _read_excel_sheet(content, engine="calamine")
    except ImportError:
        pass  # 本機沒裝 python-calamine，退回 openpyxl
    except Exception:
        pass  # calamine 讀取失敗（罕見），也退回 openpyxl 再試一次
    return _read_excel_sheet(content, engine="openpyxl")


def _read_upload(filename: str, content: bytes) -> pd.DataFrame:
    lower = filename.lower()
    if lower.endswith((".xlsx", ".xlsm")):
        return _read_excel_robust(content)
    if lower.endswith(".csv"):
        return pd.read_csv(io.BytesIO(content), dtype=object)
    raise ValueError("僅支援 .xlsx／.xlsm／.csv 檔案")


def _preview(df: pd.DataFrame, n: int = 20):
    cols = [str(c).strip() for c in df.columns]
    head = df.head(n).astype(object).where(pd.notna(df.head(n)), None)
    missing = [c for c in ORDER_COLUMNS if c not in cols]
    return {
        "columns": cols,
        "row_count": int(len(df)),
        "missing_required_columns": missing,
        "preview_rows": head.values.tolist(),
    }


def _process_upload(session_id: str, upload_id: int, filename: str, content: bytes) -> None:
    """在背景執行緒跑實際讀檔（可能要跑好幾分鐘），完成後把結果落地並更新 upload 紀錄。"""
    sess = state.get_session(session_id)
    try:
        df = _read_upload(filename, content)
        sess.raw_df = df
        sess.load_error = None
        state.persist_raw(sess)                                  # 解析後的 DataFrame 落地，重啟不必重讀檔
        store.finish_upload(upload_id, "ready", row_count=int(len(df)))
        # status 最後才翻成 ready：確保前端一旦看到 status=ready，uploads 紀錄也已是最終狀態
        # （否則會出現「status 已 ready 但檔案紀錄還顯示 processing」的短暫不一致）。
        sess.status = "ready"
        state.persist_meta(sess)
    except Exception as e:
        msg = f"{type(e).__name__}：{e}"
        state.clear_raw(sess)
        store.finish_upload(upload_id, "error", error=msg)
        sess.status = "error"
        sess.load_error = msg
        state.persist_meta(sess)


@router.post("/upload")
async def upload(background_tasks: BackgroundTasks, file: UploadFile = File(...),
                  session_id: str = Depends(get_session_id)):
    filename = file.filename or ""
    if not filename.lower().endswith((".xlsx", ".xlsm", ".csv")):
        raise HTTPException(status_code=400, detail="僅支援 .xlsx／.xlsm／.csv 檔案")
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="檔案是空的")

    sess = state.get_session(session_id)
    # 先把使用者上傳的原始檔位元組落地保存一份、並在 uploads 表登記（status=processing），
    # 這就是「系統記錄他們的檔案」：不論後續解析成敗，這筆上傳紀錄與原始檔都會留著。
    upload_id, _ = store.record_upload(sess.session_id, filename, content)

    state.clear_raw(sess)               # 換新檔，舊的原始資料失效，先清掉避免重啟載回舊資料
    state.clear_clean(sess)             # 先前的清洗結果也失效，需重新呼叫 /api/clean/run
    sess.raw_filename = filename
    sess.status = "processing"
    sess.load_error = None
    state.persist_meta(sess)

    background_tasks.add_task(_process_upload, sess.session_id, upload_id, filename, content)

    return {
        "status": "processing",
        "filename": filename,
        "upload_id": upload_id,
        "message": "檔案已接收，正在背景解析。資料量大（例如百萬列等級）時可能需要數分鐘，"
                    "請改用 GET /api/ingest/status 輪詢進度，不必等這支 API 本身回應。",
    }


@router.post("/sample")
def load_sample(session_id: str = Depends(get_session_id)):
    """載入內建個案範例資料（節錄自 P零售物流data.xlsx，供沒有原始檔時展示用）。資料量小，同步處理即可。"""
    with open(_SAMPLE_PATH, encoding="utf-8") as f:
        fixture = json.load(f)
    df = pd.DataFrame(fixture["rows"], columns=fixture["headers"])

    sess = state.get_session(session_id)
    sess.raw_df = df
    sess.raw_filename = "（內建個案範例資料）"
    sess.status = "ready"
    sess.load_error = None
    state.clear_clean(sess)        # 換資料，先前清洗結果失效
    state.persist_raw(sess)        # 範例資料也落地，重啟後仍在
    state.persist_meta(sess)

    return jsonsafe.clean({"filename": sess.raw_filename, "status": "ready", **_preview(df)})


@router.get("/status")
def status(session_id: str = Depends(get_session_id)):
    sess = state.get_session(session_id)
    resp = {"status": sess.status, "filename": sess.raw_filename}
    if sess.status == "error":
        resp["error"] = sess.load_error
    elif sess.status == "ready" and sess.raw_df is not None:
        resp["cleaned"] = sess.cleaning_result is not None
        resp.update(_preview(sess.raw_df))
    # status == "idle" 或 "processing" 時就只回上面兩個欄位，前端據此顯示對應訊息。
    return jsonsafe.clean(resp)


@router.get("/uploads")
def uploads(session_id: str = Depends(get_session_id)):
    """列出這個 session 上傳過的所有檔案（檔名、大小、列數、狀態、時間）。
    對應「系統能記錄他們的檔案」——重啟後仍查得到，因為紀錄存在 SQLite。"""
    sess = state.get_session(session_id)
    return jsonsafe.clean({"uploads": store.list_uploads(sess.session_id)})
