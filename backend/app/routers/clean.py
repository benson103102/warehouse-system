"""資料清洗端點 — 對應原前端 runCleaningPipeline()／renderCleanResults()。"""

import os
import tempfile

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from .. import jsonsafe, state
from ..deps import get_session_id, require_clean_result
from ..services.cleaning_core import run_cleaning_pipeline, export_cleaned_xlsx

router = APIRouter(prefix="/api/clean", tags=["clean"])


def _summary(sess: state.SessionState):
    r = sess.cleaning_result
    iso_reason_counts = (
        r.iso_df["隔離主要原因"].value_counts().to_dict() if len(r.iso_df) else {}
    )
    material_source_counts = r.clean_df["材積來源"].value_counts().to_dict()
    storage_class_counts = r.sku_dim_df["儲位分類"].value_counts().to_dict()
    return {
        "stats": r.stats,
        "iso_reason_counts": iso_reason_counts,
        "material_source_counts": material_source_counts,
        "storage_class_counts": storage_class_counts,
        "change_log_count": int(len(r.change_log_df)),
        "address_pending_confirm": int(r.clean_df["配送地址_待確認_flag"].sum()),
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
    """下載清洗結果（沿用原 write_output() 的分頁格式，供匯出/複核用）。"""
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
