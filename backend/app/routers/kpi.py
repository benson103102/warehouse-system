"""
KPI 儀表板端點 — 對應原前端 runStorageSimulation()。串接批次併單模擬＋儲位配置改善，
算出移動距離／工時改善率，並與專案章程訂下的三項 KPI 目標比對。
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query

from .. import jsonsafe, state
from ..deps import require_clean_result
from ..services import analytics as an
from ..services.cleaning_core import CATEGORY_NAME_BY_ID

router = APIRouter(prefix="/api/kpi", tags=["kpi"])


@router.get("/compute")
def compute(capacity: int = Query(20, ge=1, le=500),
            unit_count: int = Query(3, ge=1, le=50),
            speed: float = Query(1.0, gt=0),
            handle_sec: float = Query(12.0, ge=0),
            mode: str = Query("manual", pattern="^(manual|agv)$"),
            a_thresh: float = Query(70, ge=0, le=100), b_thresh: float = Query(90, ge=0, le=100),
            sections: Optional[str] = None, materials: Optional[str] = None,
            sess: state.SessionState = Depends(require_clean_result)):
    sec_set = set(sections.split(",")) if sections else None
    mat_set = set(materials.split(",")) if materials else None

    # 拉「速度／人數／取放秒數」等滑桿時，只有最後的 compute_kpi 會變；下面的頻次與批次模擬
    # 都與那些滑桿無關，靠快取重用，不再每次重算（批次以 capacity 當 key，改容量才會重算）。
    freq = sess.cached(("freq",), lambda: an.sku_frequency(sess.cleaning_result.clean_df))
    items = freq["items"] if freq else []
    storage = an.compute_storage_assignment(items, CATEGORY_NAME_BY_ID, sess.zones,
                                             a_thresh, b_thresh, sec_set, mat_set)
    batch = sess.cached(("batch", capacity),
                        lambda: an.batch_simulate(sess.cleaning_result.clean_df, capacity))
    kpi = an.compute_kpi(storage, batch, unit_count, speed, handle_sec, mode)
    return jsonsafe.clean(kpi)
