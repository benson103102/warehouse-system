"""
僅供本機驗證用的「同路徑／同回傳格式」Starlette 版鏡射伺服器 —— 不是要交付給使用者的
程式碼。因為這個沙盒環境的網路政策擋掉了 PyPI（連 pip/uv/apt 都連不到 files.pythonhosted.org），
沒辦法在這裡直接把 FastAPI 裝起來、實際啟動 app/main.py 做端對端測試。

但 Starlette／pydantic／uvicorn／python-multipart 這些 FastAPI 的底層套件恰好已經預裝在
這個環境，於是用它們刻一個「路徑、查詢參數、回傳 JSON 形狀」都跟正式的 FastAPI 版本
一致的最簡版本，藉此驗證：
  1. 業務邏輯（cleaning_core／analytics）透過真正的 HTTP 請求（multipart 上傳、query string、
     JSON 回應）跑起來沒問題 —— 這是最容易出錯、也最重要的一層。
  2. 前端 fetch 呼叫的每個路徑／參數名稱／回傳欄位名稱，跟後端實際吐出來的完全對得上
     （這是 frontend/index.html 直接依賴的「契約」）。
使用者實際執行時，請照 README 指示 `pip install -r requirements.txt` 後用
`uvicorn app.main:app` 啟動正式的 FastAPI 版本，不要用這支鏡射伺服器。
"""

import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import io
import threading

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route

from app import jsonsafe, state
from app.services import analytics as an
from app.services.cleaning_core import CATEGORY_NAME_BY_ID, ORDER_COLUMNS, run_cleaning_pipeline, export_cleaned_xlsx  # noqa: F401

# 注意：這裡刻意「複製」而不是 import app.routers.ingest 裡的 _read_upload／_preview——
# 因為 app/routers/ingest.py 頂部 import fastapi，這個沙盒裝不了 fastapi，一 import 就會炸掉。
# 正式環境沒有這個限制，直接看 app/routers/ingest.py 的版本即可（邏輯完全一致）。


def _read_excel_sheet(content, engine):
    import pandas as pd
    try:
        return pd.read_excel(io.BytesIO(content), sheet_name="訂單資料", dtype=object, engine=engine)
    except ValueError as e:
        if "Worksheet" in str(e) or "訂單資料" in str(e):
            return pd.read_excel(io.BytesIO(content), sheet_name=0, dtype=object, engine=engine)
        raise


def _read_excel_robust(content):
    try:
        return _read_excel_sheet(content, engine="calamine")
    except ImportError:
        pass
    except Exception:
        pass
    return _read_excel_sheet(content, engine="openpyxl")


def _read_upload(filename, content):
    import pandas as pd
    lower = filename.lower()
    if lower.endswith((".xlsx", ".xlsm")):
        return _read_excel_robust(content)
    if lower.endswith(".csv"):
        return pd.read_csv(io.BytesIO(content), dtype=object)
    raise ValueError("僅支援 .xlsx／.xlsm／.csv 檔案")


def _preview(df, n=20):
    import pandas as pd
    cols = [str(c).strip() for c in df.columns]
    head = df.head(n).astype(object).where(pd.notna(df.head(n)), None)
    missing = [c for c in ORDER_COLUMNS if c not in cols]
    return {
        "columns": cols,
        "row_count": int(len(df)),
        "missing_required_columns": missing,
        "preview_rows": head.values.tolist(),
    }


def sess():
    return state.get_session(state.DEFAULT_SESSION_ID)


async def health(request):
    return JSONResponse({"status": "ok"})


def _process_upload_bg(session_id, filename, content):
    s = state.get_session(session_id)
    try:
        df = _read_upload(filename, content)
        s.raw_df = df
        s.status = "ready"
        s.load_error = None
    except Exception as e:
        s.raw_df = None
        s.status = "error"
        s.load_error = f"{type(e).__name__}：{e}"


async def ingest_upload(request):
    form = await request.form()
    file = form["file"]
    content = await file.read()
    filename = file.filename
    s = sess()
    s.raw_df = None
    s.raw_filename = filename
    s.cleaning_result = None
    s.status = "processing"
    s.load_error = None
    # 跟正式版一樣丟到背景執行緒跑，立刻回應，驗證輪詢流程本身也走得通。
    threading.Thread(target=_process_upload_bg, args=(state.DEFAULT_SESSION_ID, filename, content), daemon=True).start()
    return JSONResponse({"status": "processing", "filename": filename})


async def ingest_sample(request):
    import pandas as pd
    fixture_path = os.path.join(os.path.dirname(__file__), "..", "app", "services", "fixtures", "sample_orders.json")
    with open(fixture_path, encoding="utf-8") as f:
        fixture = json.load(f)
    df = pd.DataFrame(fixture["rows"], columns=fixture["headers"])
    s = sess()
    s.raw_df = df
    s.raw_filename = "sample"
    s.cleaning_result = None
    s.status = "ready"
    s.load_error = None
    return JSONResponse(jsonsafe.clean({"filename": s.raw_filename, "status": "ready", "row_count": len(df)}))


async def ingest_status(request):
    s = sess()
    resp = {"status": s.status, "filename": s.raw_filename}
    if s.status == "error":
        resp["error"] = s.load_error
    elif s.status == "ready" and s.raw_df is not None:
        resp["cleaned"] = s.cleaning_result is not None
        resp.update(_preview(s.raw_df))
    return JSONResponse(jsonsafe.clean(resp))


async def clean_summary(request):
    s = sess()
    if s.cleaning_result is None:
        return JSONResponse({"detail": "not cleaned"}, status_code=409)
    r = s.cleaning_result
    return JSONResponse(jsonsafe.clean({
        "stats": r.stats,
        "iso_reason_counts": r.iso_df["隔離主要原因"].value_counts().to_dict() if len(r.iso_df) else {},
        "material_source_counts": r.clean_df["材積來源"].value_counts().to_dict(),
        "storage_class_counts": r.sku_dim_df["儲位分類"].value_counts().to_dict(),
        "change_log_count": int(len(r.change_log_df)),
        "address_pending_confirm": int(r.clean_df["配送地址_待確認_flag"].sum()),
    }))


async def clean_run(request):
    s = sess()
    if s.raw_df is None:
        return JSONResponse({"detail": "no data"}, status_code=409)
    s.cleaning_result = run_cleaning_pipeline(s.raw_df)
    return JSONResponse(jsonsafe.clean({"stats": s.cleaning_result.stats}))


def _require_clean():
    s = sess()
    if s.cleaning_result is None:
        raise RuntimeError("not cleaned")
    return s


async def abc_sku(request):
    s = _require_clean()
    a = float(request.query_params.get("a_thresh", 70))
    b = float(request.query_params.get("b_thresh", 90))
    limit = int(request.query_params.get("limit", 200))
    freq = an.sku_frequency(s.cleaning_result.clean_df)
    meta = an.apply_abc_thresholds(freq["items"], a, b)
    a_n = sum(1 for i in freq["items"] if i["cls"] == "A")
    b_n = sum(1 for i in freq["items"] if i["cls"] == "B")
    c_n = len(freq["items"]) - a_n - b_n
    return JSONResponse(jsonsafe.clean({
        "items": freq["items"][:limit], "total_sku_count": len(freq["items"]),
        "class_counts": {"A": a_n, "B": b_n, "C": c_n}, "meta": meta,
    }))


async def abc_category(request):
    s = _require_clean()
    a = float(request.query_params.get("a_thresh", 70))
    b = float(request.query_params.get("b_thresh", 90))
    freq = an.sku_frequency(s.cleaning_result.clean_df)
    cats = an.category_abc_from_sku(freq["items"], CATEGORY_NAME_BY_ID, a, b)
    return JSONResponse(jsonsafe.clean({"categories": cats}))


async def copick_sku(request):
    s = _require_clean()
    sort = request.query_params.get("sort", "count")
    limit = int(request.query_params.get("limit", 50))
    freq = an.sku_frequency(s.cleaning_result.clean_df)
    result = an.sku_copick(freq)
    pairs = sorted(result["pairs"], key=lambda p: -p[sort])[:limit]
    return JSONResponse(jsonsafe.clean({"pairs": pairs, "total_pairs": len(result["pairs"])}))


async def copick_category(request):
    s = _require_clean()
    cap_n = int(request.query_params.get("cap_n", 20))
    result = an.category_copick(s.cleaning_result.clean_df, CATEGORY_NAME_BY_ID, cap_n)
    return JSONResponse(jsonsafe.clean(result))


async def forecast_series(request):
    s = _require_clean()
    granularity = request.query_params.get("granularity", "month")
    target = request.query_params.get("target", "qty")
    sku_id = request.query_params.get("sku_id")
    agg = an.compute_forecast_agg(s.cleaning_result.clean_df, granularity)
    test_len = an.FC_TEST_MONTH if granularity == "month" else an.FC_TEST_WEEK
    if sku_id is not None:
        key_map = agg["by_sku_cnt"] if target == "cnt" else agg["by_sku"]
        actual = an.series_for(key_map, agg["periods"], key=int(sku_id))
    else:
        key_map = agg["overall_cnt"] if target == "cnt" else agg["overall"]
        actual = an.series_for(key_map, agg["periods"])
    v = an.fc_validate(actual, test_len)
    return JSONResponse(jsonsafe.clean({
        "periods": agg["periods"], "test_len": test_len, "actual": actual,
        "model": v["model"], "pred_tail": v["pred"], "mase": v["mase"], "wape": v["wape"],
    }))


async def forecast_breakdown(request):
    s = _require_clean()
    granularity = request.query_params.get("granularity", "month")
    target = request.query_params.get("target", "qty")
    agg = an.compute_forecast_agg(s.cleaning_result.clean_df, granularity)
    test_len = an.FC_TEST_MONTH if granularity == "month" else an.FC_TEST_WEEK
    key_map = agg["by_cat_cnt"] if target == "cnt" else agg["by_cat"]
    rows = []
    for cat in agg["by_cat"].index.get_level_values(0).unique():
        sr = an.series_for(key_map, agg["periods"], key=cat)
        if len(sr) < test_len + 4:
            continue
        v = an.fc_validate(sr, test_len)
        rows.append({"cat": cat, "name": an.category_label(cat, CATEGORY_NAME_BY_ID),
                     "actual_sum": sum(v["actual"]), "pred_sum": sum(v["pred"]),
                     "wape": v["wape"], "mase": v["mase"], "model": v["model"]})
    rows.sort(key=lambda r: -r["actual_sum"])
    return JSONResponse(jsonsafe.clean({"rows": rows}))


async def forecast_lifecycle(request):
    s = _require_clean()
    granularity = request.query_params.get("granularity", "month")
    recent_n = int(request.query_params.get("recent_n", 2))
    agg = an.compute_forecast_agg(s.cleaning_result.clean_df, granularity)
    result = an.lifecycle_classify(agg, recent_n)
    return JSONResponse(jsonsafe.clean(result))


async def batch_simulate(request):
    s = _require_clean()
    capacity = int(request.query_params.get("capacity", 20))
    result = an.batch_simulate(s.cleaning_result.clean_df, capacity)
    return JSONResponse(jsonsafe.clean(result))


async def storage_assignment(request):
    s = _require_clean()
    a = float(request.query_params.get("a_thresh", 70))
    b = float(request.query_params.get("b_thresh", 90))
    freq = an.sku_frequency(s.cleaning_result.clean_df)
    result = an.compute_storage_assignment(freq["items"], CATEGORY_NAME_BY_ID, s.zones, a, b)
    categories = [{k: v for k, v in c.items() if k != "cells"} for c in result["categories"]]
    return JSONResponse(jsonsafe.clean({
        "categories": categories, "baseline_dist_m": result["baseline"],
        "weighted_dist_m": result["weighted"], "improvement_pct": result["improvement"],
        "coverage_pct": result["coverage"], "pool_size": result["pool_size"],
    }))


async def kpi_compute(request):
    s = _require_clean()
    capacity = int(request.query_params.get("capacity", 20))
    unit_count = int(request.query_params.get("unit_count", 3))
    speed = float(request.query_params.get("speed", 1.0))
    handle_sec = float(request.query_params.get("handle_sec", 12.0))
    mode = request.query_params.get("mode", "manual")
    a = float(request.query_params.get("a_thresh", 70))
    b = float(request.query_params.get("b_thresh", 90))
    freq = an.sku_frequency(s.cleaning_result.clean_df)
    storage = an.compute_storage_assignment(freq["items"], CATEGORY_NAME_BY_ID, s.zones, a, b)
    batch = an.batch_simulate(s.cleaning_result.clean_df, capacity)
    kpi = an.compute_kpi(storage, batch, unit_count, speed, handle_sec, mode)
    return JSONResponse(jsonsafe.clean(kpi))


routes = [
    Route("/api/health", health),
    Route("/api/ingest/upload", ingest_upload, methods=["POST"]),
    Route("/api/ingest/sample", ingest_sample, methods=["POST"]),
    Route("/api/ingest/status", ingest_status),
    Route("/api/clean/run", clean_run, methods=["POST"]),
    Route("/api/clean/summary", clean_summary),
    Route("/api/abc/sku", abc_sku),
    Route("/api/abc/category", abc_category),
    Route("/api/copick/sku", copick_sku),
    Route("/api/copick/category", copick_category),
    Route("/api/forecast/series", forecast_series),
    Route("/api/forecast/breakdown", forecast_breakdown),
    Route("/api/forecast/lifecycle", forecast_lifecycle),
    Route("/api/batch/simulate", batch_simulate),
    Route("/api/storage/assignment", storage_assignment),
    Route("/api/kpi/compute", kpi_compute),
]

mirror_app = Starlette(routes=routes, middleware=[
    Middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]),
])

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(mirror_app, host="127.0.0.1", port=8000)
