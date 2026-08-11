"""共同揀取（市場籃）分析端點 — 對應原前端 computeSkuCoPick()／computeCategoryCoPick()。"""

from fastapi import APIRouter, Depends, Query

from .. import jsonsafe, state
from ..deps import require_clean_result
from ..services import analytics as an
from ..services.cleaning_core import CATEGORY_NAME_BY_ID

router = APIRouter(prefix="/api/copick", tags=["copick"])


@router.get("/sku")
def sku_copick(sort: str = Query("count", pattern="^(count|lift)$"), limit: int = Query(50, ge=1, le=500),
               sess: state.SessionState = Depends(require_clean_result)):
    freq = sess.cached(("freq",), lambda: an.sku_frequency(sess.cleaning_result.clean_df))
    if not freq:
        return jsonsafe.clean({"pairs": []})
    # 共同揀取矩陣只依賴清洗結果，跟 sort／limit 無關，快取起來；切換排序、改筆數都免重算。
    result = sess.cached(("sku_copick",), lambda: an.sku_copick(freq))
    pairs = sorted(result["pairs"], key=lambda p: -p[sort])[:limit]
    return jsonsafe.clean({
        "pairs": pairs, "total_pairs": len(result["pairs"]),
        "count_thresh": result["count_thresh"], "lift_count_thresh": result["lift_count_thresh"],
        "total_orders": result["total_orders"],
    })


@router.get("/category")
def category_copick(cap_n: int = Query(20, ge=2, le=69),
                     sess: state.SessionState = Depends(require_clean_result)):
    result = sess.cached(("cat_copick", cap_n),
                         lambda: an.category_copick(sess.cleaning_result.clean_df, CATEGORY_NAME_BY_ID, cap_n))
    if not result:
        return jsonsafe.clean({"labels": [], "matrix": [], "lift_matrix": []})
    return jsonsafe.clean(result)
