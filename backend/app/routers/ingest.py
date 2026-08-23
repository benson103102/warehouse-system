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

【2026-08 追加：大檔案直傳（繞過 Cloud Run 32MB 請求上限）】
POST /api/ingest/upload 這條路是「瀏覽器把整個檔案塞進一個 HTTP 請求」直接打給後端，
本機/大部分主機沒問題，但部署到 Cloud Run 後，Cloud Run 前面的反向代理對單一請求
的大小有寫死的 32MB 硬上限（跟記憶體、CPU、逾時設定都無關，也無法調高），超過就在
還沒進到這支程式碼之前，就被 Cloud Run 直接擋下來回 413。真實資料集的「訂單資料」
工作表常常破百萬列，xlsx 檔案很容易超過 32MB。

所以新增一條「直傳」路徑，繞過 Cloud Run 這個限制：
  1. POST /api/ingest/upload_url：後端跟 Google Cloud Storage 要一個「有時效的直傳
     網址」（signed URL）回給前端。
  2. 前端瀏覽器直接把檔案 PUT 給那個網址——這個請求是「瀏覽器 → Cloud Storage」，
     完全不經過 Cloud Run，所以沒有 32MB 限制。
  3. POST /api/ingest/upload_from_gcs：前端上傳完後通知後端「檔案在 Cloud Storage
     這個位置了」，後端從 Cloud Storage 把內容讀進來（伺服器對伺服器的 API 呼叫，
     一樣不受那個 32MB 限制），走跟原本一樣的背景解析流程。

這條路徑需要環境變數 WAREHOUSE_UPLOAD_BUCKET（Cloud Storage 桶名）才會啟用；本機
開發沒設定時，/upload_url 會回 501，前端會自動退回原本的 /upload 路徑（小檔案本來
就不會踩到 32MB 上限，兩條路徑並存、互不影響）。
"""

import io
import json
import os
import uuid
from datetime import timedelta
from typing import Dict, Optional

import pandas as pd
from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from .. import jsonsafe, state, store
from ..deps import get_session_id
from ..services.cleaning_core import ORDER_COLUMNS

router = APIRouter(prefix="/api/ingest", tags=["ingest"])

_SAMPLE_PATH = os.path.join(os.path.dirname(__file__), "..", "services", "fixtures", "sample_orders.json")

# 大檔案直傳用的 Cloud Storage 桶名；沒設定就代表「這個部署環境不支援直傳」（例如本機開發）。
_UPLOAD_BUCKET = os.environ.get("WAREHOUSE_UPLOAD_BUCKET", "").strip()
_GCS_CONTENT_TYPE = "application/octet-stream"

_gcs_client = None


def _gcs():
    """延遲載入 google-cloud-storage client（本機沒裝這個套件、沒設定桶名時完全不會匯入到，
    避免沒用到大檔案直傳功能的人（例如本機開發）也要裝這個額外依賴才能跑起來）。"""
    global _gcs_client
    if _gcs_client is None:
        from google.cloud import storage as gcs_storage
        _gcs_client = gcs_storage.Client()
    return _gcs_client


def _read_excel_sheet(content: bytes, engine: str) -> pd.DataFrame:
    try:
        return pd.read_excel(io.BytesIO(content), sheet_name="訂單資料", dtype=object, engine=engine)
    except ValueError as e:
        # 找不到「訂單資料」工作表時，退而使用第一個工作表；其餘 ValueError（例如
        # 檔案本身不是合法 xlsx）原樣往上丟。
        if "Worksheet" in str(e) or "訂單資料" in str(e):
            return pd.read_excel(io.BytesIO(content), sheet_name=0, dtype=object, engine=engine)
        raise


def _read_excel_robust(content: bytes, filename: str) -> pd.DataFrame:
    """優先 calamine（Rust 實作，.xlsx／.xlsm／.xls 都支援、速度快）；沒裝或讀取失敗時，
    .xls（舊版二進位格式）退回 xlrd，其餘（.xlsx／.xlsm）退回 openpyxl——openpyxl 不支援
    讀取 .xls，所以副檔名要分開處理，避免對 .xls 檔案誤用 openpyxl 導致必然失敗。
    對應原清洗腳本的 read_excel_robust()（2026-08 追加 .xls 支援）。"""
    try:
        return _read_excel_sheet(content, engine="calamine")
    except ImportError:
        pass  # 本機沒裝 python-calamine，退回下面的備援 engine
    except Exception:
        pass  # calamine 讀取失敗（罕見），也退回下面的備援 engine 再試一次
    fallback_engine = "xlrd" if filename.lower().endswith(".xls") else "openpyxl"
    return _read_excel_sheet(content, engine=fallback_engine)


def _read_upload(filename: str, content: bytes) -> pd.DataFrame:
    lower = filename.lower()
    if lower.endswith((".xlsx", ".xlsm", ".xls")):
        return _read_excel_robust(content, filename)
    if lower.endswith(".csv"):
        return pd.read_csv(io.BytesIO(content), dtype=object)
    raise ValueError("僅支援 .xlsx／.xlsm／.xls／.csv 檔案")


def _suggest_mapping(cols):
    """欄位對應下拉選單的預設選項：必要欄位若能在檔案欄位中找到完全相同（去除前後空白後）
    的名稱，就直接建議對應到它；找不到就回傳 None，由使用者自行從下拉選單挑選。"""
    col_set = set(cols)
    return {req: (req if req in col_set else None) for req in ORDER_COLUMNS}


def _preview(df: pd.DataFrame, n: int = 20):
    cols = [str(c).strip() for c in df.columns]
    head = df.head(n).astype(object).where(pd.notna(df.head(n)), None)
    missing = [c for c in ORDER_COLUMNS if c not in cols]
    return {
        "columns": cols,
        "row_count": int(len(df)),
        "missing_required_columns": missing,
        "preview_rows": head.values.tolist(),
        "suggested_mapping": _suggest_mapping(cols),
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


def _start_processing(session_id: str, filename: str, content: bytes, background_tasks: BackgroundTasks) -> dict:
    """/upload 與 /upload_from_gcs 共用的「登記＋丟背景解析」流程，避免兩條路徑各寫一份。"""
    if not filename.lower().endswith((".xlsx", ".xlsm", ".xls", ".csv")):
        raise HTTPException(status_code=400, detail="僅支援 .xlsx／.xlsm／.xls／.csv 檔案")
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


@router.post("/upload")
async def upload(background_tasks: BackgroundTasks, file: UploadFile = File(...),
                  session_id: str = Depends(get_session_id)):
    filename = file.filename or ""
    if not filename.lower().endswith((".xlsx", ".xlsm", ".xls", ".csv")):
        raise HTTPException(status_code=400, detail="僅支援 .xlsx／.xlsm／.xls／.csv 檔案")
    content = await file.read()
    return _start_processing(session_id, filename, content, background_tasks)


class UploadUrlRequest(BaseModel):
    filename: str


class UploadFromGcsRequest(BaseModel):
    object_name: str
    filename: Optional[str] = None


@router.post("/upload_url")
def create_upload_url(payload: UploadUrlRequest, session_id: str = Depends(get_session_id)):
    """給大檔案（部署在 Cloud Run 上、超過 32MB 那種）用：回傳一個有時效的 Cloud Storage
    直傳網址，前端瀏覽器直接把檔案 PUT 給這個網址，完全繞過後端、不受 Cloud Run 的請求
    大小上限限制。上傳完後，前端要接著呼叫 /api/ingest/upload_from_gcs 通知後端處理。"""
    if not _UPLOAD_BUCKET:
        raise HTTPException(status_code=501, detail="伺服器尚未設定 WAREHOUSE_UPLOAD_BUCKET 環境變數，"
                                                      "無法使用大檔案直傳（請改用一般上傳，或參考部署教學設定）")
    filename = payload.filename or ""
    if not filename.lower().endswith((".xlsx", ".xlsm", ".xls", ".csv")):
        raise HTTPException(status_code=400, detail="僅支援 .xlsx／.xlsm／.xls／.csv 檔案")

    # 路徑帶上 session_id：一來檔案彼此不會互相覆蓋，二來 /upload_from_gcs 可以用這個前綴
    # 確認「這個物件真的是這位登入者自己上傳的」，避免有人猜物件名稱去讀別人上傳的檔案。
    object_name = f"uploads-staging/{session_id}/{uuid.uuid4().hex}_{filename}"
    blob = _gcs().bucket(_UPLOAD_BUCKET).blob(object_name)

    # Cloud Run／GCE 上沒有本機私鑰可以簽章，client library 改用「臨時借用執行身分的
    # access token」透過 IAM Credentials API 簽 v4 signed URL——這是 Google 官方文件記載
    # 的標準做法，執行身分（Cloud Run 的服務帳戶）需要對自己有 Service Account Token
    # Creator 角色，並啟用 IAM Service Account Credentials API（部署教學會附上設定指令）。
    import google.auth
    from google.auth.transport import requests as google_auth_requests

    credentials, _ = google.auth.default()
    credentials.refresh(google_auth_requests.Request())

    url = blob.generate_signed_url(
        version="v4",
        expiration=timedelta(minutes=30),
        method="PUT",
        content_type=_GCS_CONTENT_TYPE,
        service_account_email=credentials.service_account_email,
        access_token=credentials.token,
    )
    return {"upload_url": url, "object_name": object_name, "content_type": _GCS_CONTENT_TYPE}


@router.post("/upload_from_gcs")
async def upload_from_gcs(payload: UploadFromGcsRequest, background_tasks: BackgroundTasks,
                           session_id: str = Depends(get_session_id)):
    """前端把檔案直傳到 Cloud Storage 之後呼叫這支：後端從 Cloud Storage 讀出內容，
    走跟 /upload 完全一樣的落地＋背景解析流程。"""
    if not _UPLOAD_BUCKET:
        raise HTTPException(status_code=501, detail="伺服器尚未設定 WAREHOUSE_UPLOAD_BUCKET 環境變數")
    object_name = payload.object_name or ""
    if not object_name.startswith(f"uploads-staging/{session_id}/"):
        raise HTTPException(status_code=403, detail="object_name 不屬於目前登入的使用者")
    filename = payload.filename or object_name.rsplit("/", 1)[-1].split("_", 1)[-1]

    blob = _gcs().bucket(_UPLOAD_BUCKET).blob(object_name)
    # google-cloud-storage 是同步套件，exists()／download_as_bytes() 對 90MB 檔案來說要跑
    # 個幾秒，用 run_in_threadpool 丟到背景執行緒跑，避免這幾秒卡住其他人打進來的請求
    # （這支端點本身還是 async def，事件迴圈不能被同步的網路呼叫卡住）。
    if not await run_in_threadpool(blob.exists):
        raise HTTPException(status_code=404, detail="Cloud Storage 上找不到這個檔案，請確認直傳是否成功後再試一次")
    content = await run_in_threadpool(blob.download_as_bytes)

    result = _start_processing(session_id, filename, content, background_tasks)
    # 暫存檔已經讀進來、也落地存了一份（見 _start_processing 內的 store.record_upload），
    # Cloud Storage 上的暫存物件用完即丟，背景清掉即可，就算刪除失敗也不影響匯入結果
    # （之後可以另外幫這個桶設定生命週期規則自動清舊檔，當作保險）。
    background_tasks.add_task(_delete_staged_blob, object_name)
    return result


def _delete_staged_blob(object_name: str) -> None:
    try:
        _gcs().bucket(_UPLOAD_BUCKET).blob(object_name).delete()
    except Exception:
        pass


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


class MappingRequest(BaseModel):
    # {必要欄位名稱: 使用者選的檔案欄位名稱}；沒有要指定的必要欄位可以省略，或給 None／空字串
    # （代表「不對應」，維持原樣，該必要欄位若本來就缺，仍會缺）。
    mapping: Dict[str, Optional[str]]


@router.post("/mapping")
def apply_mapping(payload: MappingRequest, session_id: str = Depends(get_session_id)):
    """套用使用者手動調整過的欄位對應：把匯入檔案裡實際的欄位名稱，改名成清洗腳本
    （P零售物流data_清洗腳本.py）認得的 18 個固定必要欄位名稱，下游的清洗／儲位配置／
    批次併單／儲位模擬完全不用改，因為它們看到的欄位名稱從此就是標準名稱。

    對應改變等於原始資料的欄位結構變了，先前的清洗結果（若有）已不再適用，這裡跟
    /api/ingest/upload 換新檔案時一樣清掉，需重新呼叫 /api/clean/run。"""
    sess = state.get_session(session_id)
    if sess.raw_df is None:
        raise HTTPException(status_code=400, detail="尚未匯入資料，請先上傳檔案或載入範例資料")

    df = sess.raw_df
    # 前端下拉選單的選項來自 _preview() 回傳的 columns（已用 str(c).strip() 去頭尾空白），
    # 但這裡原本直接拿 df.columns（未去空白）去比對，欄位名稱若帶有前後空白（Excel／CSV
    # 常見），字串就對不起來，導致明明畫面上顯示的名稱一樣，卻報「檔案中找不到欄位」。
    # 改成同樣用去空白後的名稱來比對，並記住對回原始（未去空白）欄名，rename 才會作用在
    # 真正存在的欄位上。
    stripped_to_actual: Dict[str, str] = {}
    for c in df.columns:
        key = str(c).strip()
        stripped_to_actual.setdefault(key, c)  # 同名重複時取第一個，避免任意覆蓋

    rename: Dict[str, str] = {}
    used_sources = set()
    for req_col, src_col in payload.mapping.items():
        if req_col not in ORDER_COLUMNS:
            raise HTTPException(status_code=400, detail=f"不是有效的必要欄位：{req_col}")
        if not src_col:
            continue  # 使用者選「（未對應）」：略過，維持原樣
        actual_col = stripped_to_actual.get(str(src_col).strip())
        if actual_col is None:
            raise HTTPException(status_code=400, detail=f"檔案中找不到欄位：{src_col}")
        if actual_col in used_sources:
            raise HTTPException(status_code=400,
                                 detail=f"同一個檔案欄位（{src_col}）不能同時對應到多個必要欄位")
        used_sources.add(actual_col)
        rename[actual_col] = req_col

    # 若某個必要欄位名稱本身已存在於原始檔案中、但這次使用者選了「別的」檔案欄位對應過去，
    # 要先把那個舊的同名欄位丟掉，改名後才不會出現兩欄同名（pandas 允許但下游會取到錯的那欄）。
    drop_cols = [req for req in rename.values() if req in df.columns and req not in rename]
    if drop_cols:
        df = df.drop(columns=drop_cols)
    df = df.rename(columns=rename)

    sess.raw_df = df
    state.persist_raw(sess)
    state.clear_clean(sess)
    sess.status = "ready"
    state.persist_meta(sess)

    return jsonsafe.clean(_preview(df))


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
