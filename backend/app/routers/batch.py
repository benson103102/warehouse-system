"""
批次併單模擬端點 — /simulate 對應原前端最初版 computeBatch()（只算「回合數」改善率的
陽春版，維持不動、供只想看單一數字的呼叫端用）。

/strategies、/day_plan、/dates 對應原前端後來加強的「地理分群＋播種式批次」（見
analytics.py 第11節）：依出貨日期×配送區域分波、依實際儲位距離估算移動距離，並可查詢
特定出貨日期的詳細揀貨計畫。這幾支都要先算好「SKU→儲位分區」（compute_wh_assignment，
較貴），故用 sess.cached() 依 (a_thresh, b_thresh) 快取，同一組門檻下改容量／分波門檻
滑桿不必重跑儲位配置。
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from .. import jsonsafe, state
from ..deps import require_clean_result
from ..services import analytics as an
from ..services.cleaning_core import CATEGORY_NAME_BY_ID

router = APIRouter(prefix="/api/batch", tags=["batch"])


@router.get("/simulate")
def simulate(capacity: int = Query(20, ge=1, le=500),
             sess: state.SessionState = Depends(require_clean_result)):
    result = an.batch_simulate(sess.cleaning_result.clean_df, capacity)
    return jsonsafe.clean(result)


def _geo_context(sess: state.SessionState, a_thresh: float, b_thresh: float):
    return sess.cached(
        ("batch_geo_ctx", a_thresh, b_thresh),
        lambda: an.batch_geo_context(sess.cleaning_result.clean_df, CATEGORY_NAME_BY_ID,
                                      sess.zones, a_thresh, b_thresh))


def _orders(sess: state.SessionState):
    return sess.cached(("batch_orders",), lambda: an.build_batch_orders(sess.cleaning_result.clean_df))


@router.get("/strategies")
def strategies(capacity: int = Query(20, ge=1, le=500),
               threshold_n: int = Query(5, ge=1, le=200, description="一波訂單數達此門檻才就地併單，否則併入同日同縣市稀疏池"),
               a_thresh: float = Query(80, ge=0, le=100), b_thresh: float = Query(95, ge=0, le=100),
               sess: state.SessionState = Depends(require_clean_result)):
    """對應原前端 computeBatchStrategies()／renderBatchScreen()：依地理分群＋播種式批次，比較
    「逐單揀貨」vs「批次揀貨」的回合數／移動距離改善率，並附地理分佈 Top15。"""
    ctx = _geo_context(sess, a_thresh, b_thresh)
    orders = _orders(sess)
    result = an.compute_batch_strategies(orders, capacity, threshold_n, ctx["zone_dist"], ctx["sku_zone"],
                                          ctx["before_zone"])
    return jsonsafe.clean(result)


@router.get("/dates")
def dates(sess: state.SessionState = Depends(require_clean_result)):
    """回傳有出貨紀錄的日期清單（YYYY-MM-DD），供前端「查詢特定出貨日」下拉選單使用。"""
    orders = _orders(sess)
    ds = sorted({e["date"] for e in orders.values() if e["date"]})
    return jsonsafe.clean({"dates": [f"{d[:4]}-{d[4:6]}-{d[6:8]}" for d in ds if len(d) == 8]})


@router.get("/day_plan")
def day_plan(day: str = Query(..., description="出貨日期 YYYY-MM-DD 或 YYYYMMDD"),
             capacity: int = Query(20, ge=1, le=500),
             threshold_n: int = Query(5, ge=1, le=200),
             a_thresh: float = Query(80, ge=0, le=100), b_thresh: float = Query(95, ge=0, le=100),
             sess: state.SessionState = Depends(require_clean_result)):
    """對應原前端 computeDayPlan()／renderBatchDay()：單一出貨日的地理分波與各波 SKU 揀取
    順序（含回合切分、累計完成訂單），可直接當第一線揀貨班表使用。"""
    day_key = day.replace("-", "")
    if len(day_key) != 8 or not day_key.isdigit():
        raise HTTPException(status_code=400, detail="day 格式需為 YYYY-MM-DD 或 YYYYMMDD")
    ctx = _geo_context(sess, a_thresh, b_thresh)
    orders = _orders(sess)
    plan = an.compute_day_plan(orders, day_key, capacity, threshold_n, ctx["zone_dist"], ctx["sku_zone"],
                                ctx["sku_info"])
    if not plan:
        raise HTTPException(status_code=404, detail=f"{day} 無出貨訂單")
    return jsonsafe.clean({"day": day_key, "waves": plan})
