"""資料清洗端點 — 對應原前端 runCleaningPipeline()／renderCleanResults()。"""

import os
import tempfile

import pandas as pd
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from .. import jsonsafe, state
from ..deps import get_session_id, require_clean_result
from ..services.cleaning_core import run_cleaning_pipeline, export_cleaned_xlsx, export_cleaned_zip

router = APIRouter(prefix="/api/clean", tags=["clean"])


def _quality_scorecard(r, iso_reason_counts: dict) -> dict:
    """清洗成效摘要（覆蓋率／準確度／時效／待人工覆核）— 對應前端原型 renderCleanResults()
    的四張品質評分卡（coverage／accuracy／timeliness／reviewCount）。百分比與筆數皆在後端
    算好，前端只需直接顯示，不必再重新掃一次乾淨資料。"""
    total_before = r.stats.get("original_row_count") or 0
    total_clean = r.stats.get("clean_count") or 0
    material_review_count = int(r.stats.get("material_review_count", 0))
    reclass = r.stats.get("reclass") or {}
    still_unmatched = int(reclass.get("still_unmatched", 0))

    coverage_pct = (total_clean / total_before * 100) if total_before else 0.0

    acc_issue_count = (
        int(iso_reason_counts.get("系統出貨單號格式異常", 0))
        + int(iso_reason_counts.get("訂購數量缺漏或非正值", 0))
        + int(iso_reason_counts.get("單位缺漏或異常", 0))
        + material_review_count
    )
    accuracy_pct = max(0.0, (total_before - acc_issue_count) / total_before * 100) if total_before else 0.0

    ship = pd.to_datetime(r.clean_df["出貨日期"], errors="coerce") if "出貨日期" in r.clean_df.columns else pd.Series([], dtype="datetime64[ns]")
    checkable = ship.notna()
    checkable_count = int(checkable.sum())
    max_ship = ship.max() if checkable_count else None
    min_ship = ship.min() if checkable_count else None
    now = pd.Timestamp.now()
    future_mask = checkable & (ship > now)
    if pd.notna(max_ship):
        early_floor = max_ship - pd.DateOffset(years=5)
        outlier_mask = checkable & (ship < early_floor)
    else:
        outlier_mask = pd.Series(False, index=ship.index)
    future_count = int(future_mask.sum())
    outlier_count = int(outlier_mask.sum())
    bad_count = int((future_mask | outlier_mask).sum())
    timeliness_pct = ((checkable_count - bad_count) / checkable_count * 100) if checkable_count else 100.0
    coverage_span = None
    if pd.notna(min_ship) and pd.notna(max_ship):
        coverage_span = {"from": min_ship.strftime("%Y-%m-%d"), "to": max_ship.strftime("%Y-%m-%d")}

    review_count = material_review_count + still_unmatched
    review_pct = (review_count / total_before * 100) if total_before else 0.0

    return {
        "coverage_pct": coverage_pct,
        "accuracy_pct": accuracy_pct,
        "accuracy_issue_count": acc_issue_count,
        "timeliness_pct": timeliness_pct,
        "timeliness_checkable": checkable_count,
        "timeliness_future": future_count,
        "timeliness_outlier": outlier_count,
        "timeliness_bad": bad_count,
        "coverage_span": coverage_span,
        "review_count": review_count,
        "review_pct": review_pct,
    }


# 「配送地址_補值來源」欄裡「同門市代碼多可信版本」那一類的實際文字內容帶有逐列不同的
# 日期區間（見 cleaning_core.backfill_delivery_address 情況B，例如「…採用2024-05-01~
# 2024-11-03期間版本」），無法直接用 value_counts() 精確分組。這裡改用「前綴比對」把五種
# 情況歸桶，對應前端原型 ADDR_SOURCE_ORDER 的五個固定分類標籤。
_ADDR_SOURCE_BUCKETS = [
    ("原始完整(未變更)", "原始完整"),
    ("前綴回推／新舊制行政區更名正規化(路名門牌未更動)", "地址前綴回推"),
    ("同門市代碼單一可信版本回填", "同門市代碼單一可信版本回填"),
    ("同門市代碼多可信版本(疑似搬遷)依出貨日期最接近回填", "同門市代碼多可信版本"),
    ("無法處理(門市代碼查無可信地址,待人工確認)", "無法處理"),
]

# Part 25（離線全國路名資料回推）成功救回一筆「無法處理」列時，會在該列原本的
# 「配送地址_補值來源」文字後面加上這個後綴（見 cleaning_core.resolve_addresses_offline
# 的 fill_mask 分支），但字串前綴仍是「無法處理(門市代碼查無可信地址,待人工確認)」，
# 若沿用上面純前綴比對，這些其實已經解除待確認（配送地址_待確認_flag 已改回 0）的列
# 仍會被算進「無法處理」桶，導致這裡顯示的筆數比 address_pending_confirm（真正待確認
# 筆數）多出一截、兩者對不上。改成優先用這個後綴獨立分桶，才會與 flag 的真正待確認
# 筆數一致。
_ADDR_RESOLVED_BY_OFFLINE_SUFFIX = "；全國路名資料回推"
_ADDR_RESOLVED_BY_OFFLINE_LABEL = "全國路名資料回推補齊(原查無可信地址,已解除待確認)"


def _address_source_counts(clean_df: pd.DataFrame, iso_df: pd.DataFrame) -> dict:
    """配送地址補值來源分布 — 與材積待複核相同，在「乾淨＋隔離」全體上統計（配送地址是
    門市的屬性，跟該列是否被隔離無關），對應前端原型 addrStats.sourceCounts。"""
    col = "配送地址_補值來源"
    parts = [df[col] for df in (clean_df, iso_df) if col in df.columns and len(df)]
    if not parts:
        return {}
    combined = pd.concat(parts, ignore_index=True).fillna("").astype(str)
    counts = {label: 0 for label, _ in _ADDR_SOURCE_BUCKETS}
    counts[_ADDR_RESOLVED_BY_OFFLINE_LABEL] = 0
    other = 0
    for v in combined:
        if not v:
            continue
        if v.endswith(_ADDR_RESOLVED_BY_OFFLINE_SUFFIX):
            counts[_ADDR_RESOLVED_BY_OFFLINE_LABEL] += 1
            continue
        for label, prefix in _ADDR_SOURCE_BUCKETS:
            if v.startswith(prefix):
                counts[label] += 1
                break
        else:
            other += 1
    if other:
        counts["其他"] = other
    return {k: v for k, v in counts.items() if v > 0}


def _offline_geocode_stats(clean_df: pd.DataFrame, iso_df: pd.DataFrame) -> dict:
    """離線全國路名資料 比對概況 — 與配送地址其他統計一致，在「乾淨＋隔離」全體上計算。
    比對標籤各桶筆數（路名回推補齊／路名相符(已驗證)／未比對／未載入參考資料）。
    未載入名冊（欄位全為「未載入參考資料」）時回傳空 dict，前端可據此不顯示此區塊。"""
    col = "配送地址_離線比對"
    parts = [df[col] for df in (clean_df, iso_df) if col in df.columns and len(df)]
    if not parts:
        return {}
    combined = pd.concat(parts, ignore_index=True).fillna("").astype(str)
    counts = combined[combined != ""].value_counts().to_dict()
    if set(counts) <= {"未載入參考資料"}:
        return {}
    return {"match_counts": counts}


def _summary(sess: state.SessionState):
    r = sess.cleaning_result
    iso_reason_counts = (
        r.iso_df["隔離主要原因"].value_counts().to_dict() if len(r.iso_df) else {}
    )
    material_source_counts = r.clean_df["材積來源"].value_counts().to_dict()
    storage_class_counts = r.sku_dim_df["儲位分類"].value_counts().to_dict()
    # 配送地址待確認：在「乾淨＋隔離」全體上統計（Part 18 於 combo 上計算，兩邊都會有此欄），
    # 對應前端原型 addrUnresolvedCount（只統計乾淨資料會低估，隔離列的地址一樣需要覆核）。
    flag_col = "配送地址_待確認_flag"
    address_pending_confirm = int(r.clean_df[flag_col].sum())
    if flag_col in r.iso_df.columns and len(r.iso_df):
        address_pending_confirm += int(r.iso_df[flag_col].sum())
    return {
        "stats": r.stats,
        "iso_reason_counts": iso_reason_counts,
        "material_source_counts": material_source_counts,
        "storage_class_counts": storage_class_counts,
        "change_log_count": int(len(r.change_log_df)),
        "address_pending_confirm": address_pending_confirm,
        "address_source_counts": _address_source_counts(r.clean_df, r.iso_df),
        "offline_geocode": _offline_geocode_stats(r.clean_df, r.iso_df),
        "quality": _quality_scorecard(r, iso_reason_counts),
    }


@router.post("/run")
def run(session_id: str = Depends(get_session_id)):
    sess = state.get_session(session_id)
    if sess.status == "processing":
        raise HTTPException(status_code=409, detail="資料還在背景匯入中，請先用 GET /api/ingest/status 確認 status 已變成 ready 再執行清洗。")
    if sess.status == "error":
        raise HTTPException(status_code=409, detail=f"上次匯入失敗（{sess.load_error}），請重新呼叫 /api/ingest/upload。")
    if sess.raw_df is None:
        raise HTTPException(status_code=409, detail="尚未上傳資料，請先呼叫 /api/ingest/upload 或 /api/ingest/sample")
    sess.cleaning_result = run_cleaning_pipeline(sess.raw_df)
    state.persist_clean(sess)   # 清洗結果落地，重啟後各分析頁不必重跑清洗
    return jsonsafe.clean(_summary(sess))


@router.get("/summary")
def summary(sess: state.SessionState = Depends(require_clean_result)):
    return jsonsafe.clean(_summary(sess))


@router.get("/preview")
def preview(dataset: str = "clean", limit: int = 50, offset: int = 0,
            sess: state.SessionState = Depends(require_clean_result)):
    if dataset not in ("clean", "iso", "sku_dim", "change_log"):
        raise HTTPException(status_code=400, detail="dataset 必須是 clean／iso／sku_dim／change_log 其一")
    df = {"clean": sess.cleaning_result.clean_df, "iso": sess.cleaning_result.iso_df,
          "sku_dim": sess.cleaning_result.sku_dim_df,
          "change_log": sess.cleaning_result.change_log_df}[dataset]
    limit = max(1, min(limit, 500))
    page = df.iloc[offset:offset + limit].astype(object).where(pd.notna(df.iloc[offset:offset + limit]), None)
    return jsonsafe.clean({
        "columns": list(df.columns),
        "total_rows": int(len(df)),
        "offset": offset,
        "rows": page.values.tolist(),
    })


@router.get("/export")
def export(sess: state.SessionState = Depends(require_clean_result)):
    """下載清洗結果（沿用原 write_output() 的分頁格式，供匯出/複核用）。

    保留這支同步版本（仍輸出 xlsx）供既有的外部工具／腳本呼叫，維持相容；前端「一鍵下載
    清洗結果」按鈕走下面的 /export/start＋/export/status＋/export/download 背景流程，
    且已改為輸出 ZIP（內含 5 個 CSV），因為百萬列寫 xlsx 實測要 481 秒（見 _run_export）。"""
    fd, path = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    try:
        export_cleaned_xlsx(sess.cleaning_result, path)
    except Exception:
        os.remove(path)
        raise
    return FileResponse(
        path, filename="P零售物流data_清理結果.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        background=BackgroundTask(os.remove, path),
    )


def _run_export(session_id: str) -> None:
    """在背景執行緒產生清洗結果 ZIP（內含 5 個 CSV），完成後把暫存檔路徑與狀態寫回 session，
    供 /export/status、/export/download 讀取。

    原本這裡產生的是 xlsx，但 openpyxl 逐列寫百萬列 × 5 個工作表實測要 481 秒（約 8 分鐘），
    即使已經改成背景產生＋輪詢，使用者還是得等很久。改用 CSV 壓縮成 ZIP 後同一份資料只要
    25 秒、檔案從 121.7 MB 降到 28.3 MB（見 export_cleaned_zip）。仍保留背景＋輪詢的流程，
    因為 25 秒仍不適合讓 HTTP 請求同步卡著，資料量再放大時也還會拉長，而且前端的進度顯示
    與失敗處理都已經接在這條路上。"""
    sess = state.get_session(session_id)
    try:
        fd, path = tempfile.mkstemp(suffix=".zip")
        os.close(fd)
        export_cleaned_zip(sess.cleaning_result, path)
        old_path = sess.export_path
        sess.export_path = path
        sess.export_status = "ready"
        sess.export_error = None
        if old_path and old_path != path and os.path.exists(old_path):
            try:
                os.remove(old_path)
            except OSError:
                pass
    except Exception as e:
        sess.export_status = "error"
        sess.export_error = f"{type(e).__name__}：{e}"


@router.post("/export/start")
def export_start(background_tasks: BackgroundTasks, sess: state.SessionState = Depends(require_clean_result)):
    """啟動背景產生清洗結果 ZIP（不等待完成立刻回應），前端改用 /export/status 輪詢進度、
    完成後再呼叫 /export/download 實際取檔，避免瀏覽器一個請求卡著等。"""
    if sess.export_status == "processing":
        return jsonsafe.clean({"status": "processing", "message": "上一次匯出仍在背景進行中，請繼續輪詢 /api/clean/export/status"})
    sess.export_status = "processing"
    sess.export_error = None
    background_tasks.add_task(_run_export, sess.session_id)
    return jsonsafe.clean({
        "status": "processing",
        "message": "已在背景開始產生 ZIP 檔（內含 5 個 CSV），"
                    "請改用 GET /api/clean/export/status 輪詢，status 變成 ready 後再呼叫 GET /api/clean/export/download 取檔。",
    })


@router.get("/export/status")
def export_status(sess: state.SessionState = Depends(require_clean_result)):
    resp = {"status": sess.export_status}
    if sess.export_status == "error":
        resp["error"] = sess.export_error
    return jsonsafe.clean(resp)


@router.get("/export/download")
def export_download(sess: state.SessionState = Depends(require_clean_result)):
    if sess.export_status != "ready" or not sess.export_path or not os.path.exists(sess.export_path):
        raise HTTPException(status_code=409, detail="檔案尚未準備好，請先呼叫 POST /api/clean/export/start 並輪詢 GET /api/clean/export/status 確認 status 已變成 ready")
    return FileResponse(
        sess.export_path, filename="P零售物流data_清理結果.zip",
        media_type="application/zip",
    )
