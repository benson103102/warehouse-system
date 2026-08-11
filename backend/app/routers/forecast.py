"""
需求預測端點 — 對應原前端 computeForecastAgg()／fcValidate()／renderForecastScreen()／
computeLifecycle()。granularity=month 對齊專案 KPI 用的月粒度；week 供更細緻觀察。
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from .. import jsonsafe, state
from ..deps import require_clean_result
from ..services import analytics as an
from ..services.cleaning_core import CATEGORY_NAME_BY_ID

router = APIRouter(prefix="/api/forecast", tags=["forecast"])


def _test_len(granularity: str) -> int:
    return an.FC_TEST_MONTH if granularity == "month" else an.FC_TEST_WEEK


@router.get("/series")
def series(granularity: str = Query("month", pattern="^(month|week)$"),
           target: str = Query("qty", pattern="^(qty|cnt)$"),
           sku_id: Optional[int] = None, cat_id: Optional[int] = None,
           sess: state.SessionState = Depends(require_clean_result)):
    agg = sess.cached(("fc_agg", granularity),
                      lambda: an.compute_forecast_agg(sess.cleaning_result.clean_df, granularity))
    if not agg:
        raise HTTPException(status_code=422, detail="清洗後無有效出貨資料可供預測")
    test_len = _test_len(granularity)
    if len(agg["periods"]) < test_len + 4:
        raise HTTPException(status_code=422,
                             detail=f"資料期數不足以做最後一期驗證（至少需 {test_len + 4} 期，"
                                    f"目前僅 {len(agg['periods'])} 期）")

    if sku_id is not None:
        key_map = agg["by_sku_cnt"] if target == "cnt" else agg["by_sku"]
        actual = an.series_for(key_map, agg["periods"], key=sku_id)
        meta = agg["sku_meta"].get(sku_id)
        label = {"id": sku_id, "name": meta.get("商品名稱") if meta else None,
                 "cat": meta.get("商品類別") if meta else None}
    elif cat_id is not None:
        key_map = agg["by_cat_cnt"] if target == "cnt" else agg["by_cat"]
        actual = an.series_for(key_map, agg["periods"], key=cat_id)
        label = {"cat": cat_id, "name": an.category_label(cat_id, CATEGORY_NAME_BY_ID)}
    else:
        key_map = agg["overall_cnt"] if target == "cnt" else agg["overall"]
        actual = an.series_for(key_map, agg["periods"])
        label = {"scope": "overall"}

    if all(v == 0 for v in actual):
        raise HTTPException(status_code=404, detail="查無出貨紀錄")

    v = an.fc_validate(actual, test_len)
    return jsonsafe.clean({
        "label": label, "granularity": granularity, "target": target,
        "periods": agg["periods"], "test_len": test_len, "actual": actual,
        "model": v["model"], "pred_tail": v["pred"], "mase": v["mase"], "wape": v["wape"],
    })


@router.get("/breakdown")
def breakdown(granularity: str = Query("month", pattern="^(month|week)$"),
              target: str = Query("qty", pattern="^(qty|cnt)$"),
              sess: state.SessionState = Depends(require_clean_result)):
    """各商品類別的最後一期驗證結果彙總表（對應原 forecastCatTable）。"""
    agg = sess.cached(("fc_agg", granularity),
                      lambda: an.compute_forecast_agg(sess.cleaning_result.clean_df, granularity))
    if not agg:
        raise HTTPException(status_code=422, detail="清洗後無有效出貨資料可供預測")
    test_len = _test_len(granularity)
    if len(agg["periods"]) < test_len + 4:
        raise HTTPException(status_code=422, detail=f"資料期數不足以做最後一期驗證（至少需 {test_len + 4} 期）")

    key_map = agg["by_cat_cnt"] if target == "cnt" else agg["by_cat"]
    rows = []
    for cat in agg["by_cat"].index.get_level_values(0).unique():
        s = an.series_for(key_map, agg["periods"], key=cat)
        if len(s) < test_len + 4:
            continue
        v = an.fc_validate(s, test_len)
        rows.append({
            "cat": cat, "name": an.category_label(cat, CATEGORY_NAME_BY_ID),
            "actual_sum": sum(v["actual"]), "pred_sum": sum(v["pred"]),
            "wape": v["wape"], "mase": v["mase"], "model": v["model"],
        })
    rows.sort(key=lambda r: -r["actual_sum"])
    return jsonsafe.clean({"periods": agg["periods"], "test_len": test_len, "rows": rows})


@router.get("/sku_top")
def sku_top(granularity: str = Query("month", pattern="^(month|week)$"),
            target: str = Query("qty", pattern="^(qty|cnt)$"),
            candidate_n: int = Query(200, ge=1, le=1000),
            top_n: int = Query(20, ge=1, le=100),
            sess: state.SessionState = Depends(require_clean_result)):
    """單一 SKU 最後一期「預測 vs 實際」Top N（對應原 forecastSkuTable）。
    先取歷史出貨（量或次數）前 candidate_n 名 SKU 為候選池，各自做最後一期 hold-out 驗證，
    再依「測試期實際合計」由高到低取前 top_n 名回傳。"""
    agg = sess.cached(("fc_agg", granularity),
                      lambda: an.compute_forecast_agg(sess.cleaning_result.clean_df, granularity))
    if not agg:
        raise HTTPException(status_code=422, detail="清洗後無有效出貨資料可供預測")
    test_len = _test_len(granularity)
    if len(agg["periods"]) < test_len + 4:
        raise HTTPException(status_code=422, detail=f"資料期數不足以做最後一期驗證（至少需 {test_len + 4} 期）")

    total_map = agg["sku_total_cnt"] if target == "cnt" else agg["sku_total"]
    key_map = agg["by_sku_cnt"] if target == "cnt" else agg["by_sku"]
    candidates = [idx for idx, _ in total_map.sort_values(ascending=False).head(candidate_n).items()]

    rows = []
    for sku in candidates:
        s = an.series_for(key_map, agg["periods"], key=sku)
        if len(s) < test_len + 4:
            continue
        v = an.fc_validate(s, test_len)
        meta = agg["sku_meta"].get(sku, {})
        actual_sum = sum(v["actual"])
        pred_sum = sum(v["pred"])
        tot_err = (abs(pred_sum - actual_sum) / actual_sum * 100) if actual_sum > 0 else None
        rows.append({
            "id": sku, "code": meta.get("商品編號"), "name": meta.get("商品名稱"),
            "cat": meta.get("商品類別"), "cat_name": an.category_label(meta.get("商品類別"), CATEGORY_NAME_BY_ID),
            "actual_sum": actual_sum, "pred_sum": pred_sum,
            "wape": v["wape"], "tot_err": tot_err, "model": v["model"],
        })
    rows.sort(key=lambda r: -r["actual_sum"])
    return jsonsafe.clean({
        "periods": agg["periods"], "test_len": test_len,
        "candidate_count": len(candidates), "total_sku_count": int(len(total_map)),
        "rows": rows[:top_n],
    })


@router.get("/lifecycle")
def lifecycle(granularity: str = Query("month", pattern="^(month|week)$"), recent_n: int = Query(2, ge=1, le=12),
              sess: state.SessionState = Depends(require_clean_result)):
    agg = sess.cached(("fc_agg", granularity),
                      lambda: an.compute_forecast_agg(sess.cleaning_result.clean_df, granularity))
    if not agg:
        raise HTTPException(status_code=422, detail="清洗後無有效出貨資料可供分析")
    result = an.lifecycle_classify(agg, recent_n)
    return jsonsafe.clean(result)
