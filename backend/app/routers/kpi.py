"""
KPI 儀表板端點 — 對應原前端 runStorageSimulation()。串接『揀貨策略』頁同一套地理感知
批次模擬（逐單揀貨 vs 播種式批次揀貨），算出移動距離／回合數／工時改善率，並與專案章程
訂下的三項 KPI 目標比對。

改版重點：先前這裡是另外呼叫 compute_storage_assignment()（純儲位表平均距離，跟實際揀貨
路徑無關）＋ batch_simulate()（只用容量除總明細行數，忽略地理位置分波），兩者互不相干、
也跟畫面上寫的假設說明對不上，在某些資料分布下距離改善率會被拉到接近 0%。改為跟
/api/batch/strategies 共用同一套 compute_batch_strategies() 結果（single_before vs
seed），距離／回合數／工時三項改善率因此互相一致，也跟『揀貨策略』頁顯示的數字一致。
"""

from fastapi import APIRouter, Depends, Query

from .. import jsonsafe, state
from ..deps import require_clean_result
from ..services import analytics as an
from ..services.cleaning_core import CATEGORY_NAME_BY_ID

router = APIRouter(prefix="/api/kpi", tags=["kpi"])


def _geo_context(sess: state.SessionState, a_thresh: float, b_thresh: float):
    # 與 /api/batch/strategies 用同一把快取 key，兩頁共用同一次「儲位配置＋預測式ABC」
    # 運算結果（這支比較貴），不必各自重算一次。
    return sess.cached(
        ("batch_geo_ctx", a_thresh, b_thresh),
        lambda: an.batch_geo_context(sess.cleaning_result.clean_df, CATEGORY_NAME_BY_ID,
                                      sess.zones, a_thresh, b_thresh))


def _orders(sess: state.SessionState):
    return sess.cached(("batch_orders",), lambda: an.build_batch_orders(sess.cleaning_result.clean_df))


@router.get("/compute")
def compute(capacity: int = Query(20, ge=1, le=500),
            threshold_n: int = Query(5, ge=1, le=200,
                                      description="一波訂單數達此門檻才就地併單，否則併入同日同縣市稀疏池"),
            speed: float = Query(1.0, gt=0),
            handle_sec: float = Query(12.0, ge=0),
            a_thresh: float = Query(80, ge=0, le=100), b_thresh: float = Query(95, ge=0, le=100),
            sess: state.SessionState = Depends(require_clean_result)):
    # 拉「速度／取放秒數」等滑桿時，只有最後的 compute_kpi 會變（且只有工時改善率會變——
    # 距離／回合數改善率本來就只跟儲位配置與批次分波有關，跟這兩個滑桿無關，這是設計如此
    # 而非漏未更新，前端『揀貨策略』頁與原型的 KPI 頁行為一致）；地理配置與訂單彙總都與
    # 那些滑桿無關，靠快取重用，不再每次重算（分別以 (a_thresh,b_thresh) 及固定 key 當
    # 快取鍵）。
    ctx = _geo_context(sess, a_thresh, b_thresh)
    orders = _orders(sess)
    strategies = an.compute_batch_strategies(orders, capacity, threshold_n, ctx["zone_dist"], ctx["sku_zone"],
                                              ctx["before_zone"])
    kpi = an.compute_kpi(strategies, speed, handle_sec)
    return jsonsafe.clean(kpi)
