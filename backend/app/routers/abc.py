"""ABC 分級端點 — 對應原前端 computeSkuFreqData()＋applyAbcThresholds()／computeCategoryAbcFromSku()。"""

from fastapi import APIRouter, Depends, HTTPException, Query

from .. import jsonsafe, state
from ..deps import require_clean_result
from ..services import analytics as an
from ..services.cleaning_core import CATEGORY_NAME_BY_ID

router = APIRouter(prefix="/api/abc", tags=["abc"])


def _pareto(items, n=220):
    """把已分級（含 cum/cls）的 items 壓成一條 Pareto 曲線：對「全部」SKU 取樣 n 個點回傳
    (x=排名百分位, y=累積佔比)，並附上 A/B 分界所在的百分位（對應原前端 buildParetoPoints）。
    不受 limit 影響——曲線一定畫到 100%。"""
    total = len(items)
    if total == 0:
        return {"points": [], "a_pct": 0.0, "b_pct": 0.0}
    idxs = sorted({0, total - 1} | {min(total - 1, round(k / max(1, n - 1) * (total - 1))) for k in range(n)})
    points = [{"x": (i + 1) / total * 100, "y": items[i]["cum"]} for i in idxs]
    a_idx = next((i for i, it in enumerate(items) if it["cls"] != "A"), total)
    b_idx = next((i for i, it in enumerate(items) if it["cls"] == "C"), total)
    return {"points": points, "a_pct": a_idx / total * 100, "b_pct": b_idx / total * 100}


@router.get("/category")
def category_abc(a_thresh: float = Query(80, ge=0, le=100), b_thresh: float = Query(95, ge=0, le=100),
                  sess: state.SessionState = Depends(require_clean_result)):
    if b_thresh < a_thresh:
        raise HTTPException(status_code=400, detail="b_thresh 必須大於等於 a_thresh")
    freq = sess.cached(("freq",), lambda: an.sku_frequency(sess.cleaning_result.clean_df))
    if not freq:
        return jsonsafe.clean({"categories": []})
    # category_abc_from_sku 只讀 items、另建新結構，不會改到 items，可直接用快取。
    cats = an.category_abc_from_sku(freq["items"], CATEGORY_NAME_BY_ID, a_thresh, b_thresh)
    return jsonsafe.clean({"categories": cats})


@router.get("/predicted_category")
def predicted_category_abc(a_thresh: float = Query(80, ge=0, le=100), b_thresh: float = Query(95, ge=0, le=100),
                            sess: state.SessionState = Depends(require_clean_result)):
    """對應原前端 computePredictedCategoryAbc()／renderPredictedAbc()：用「預測值」而非歷史值
    做 ABC 分級——貨架類商品依預測揀貨次數排、棧板類依預測出貨量排。"""
    if b_thresh < a_thresh:
        raise HTTPException(status_code=400, detail="b_thresh 必須大於等於 a_thresh")
    result = an.compute_predicted_category_abc(sess.cleaning_result.clean_df, CATEGORY_NAME_BY_ID,
                                                a_thresh, b_thresh)
    if result is None:
        raise HTTPException(status_code=422, detail="清洗後無有效出貨資料可供預測")
    if result.get("insufficient"):
        raise HTTPException(status_code=422, detail="完整月數不足以預測（至少需 7 個完整月）")
    return jsonsafe.clean(result)


@router.get("/predicted_sku")
def predicted_sku_abc(a_thresh: float = Query(80, ge=0, le=100), b_thresh: float = Query(95, ge=0, le=100),
                       top_n: int = Query(15, ge=1, le=200),
                       sess: state.SessionState = Depends(require_clean_result)):
    """對應原前端 computePredictedSkuByZone()／renderPredictedSkuAbc()。predicted_sku_values()
    這段（要對每個候選 SKU 個別跑一次最後一期 hold-out 驗證，數千個 SKU 時較貴）用
    sess.cached() 存起來，拉 A/B 門檻滑桿不必重算，只有清洗結果改變才會失效重跑。"""
    if b_thresh < a_thresh:
        raise HTTPException(status_code=400, detail="b_thresh 必須大於等於 a_thresh")
    raw = sess.cached(("pred_sku_raw",), lambda: an.predicted_sku_values(sess.cleaning_result.clean_df))
    if raw.get("insufficient"):
        raise HTTPException(status_code=422, detail="完整月數不足以預測（至少需 7 個完整月）")
    result = an.classify_predicted_sku(raw, a_thresh, b_thresh)
    top = {
        "shelf": result["shelf"][:top_n], "pallet": result["pallet"][:top_n],
        "shelf_total": len(result["shelf"]), "pallet_total": len(result["pallet"]),
        "a_thresh": a_thresh, "b_thresh": b_thresh,
        # top_n（最多 200）只夠拿來畫表格；Pareto 曲線要畫到 100%，得對「全部」SKU 取樣，
        # 所以這裡另外用跟 /api/abc/sku 相同的 _pareto() 對完整（未截斷）清單取樣，不受
        # top_n 限制，前端直接拿這兩個欄位畫圖即可，不必自己用截斷後的清單湊曲線。
        "pareto_shelf": _pareto(result["shelf"]), "pareto_pallet": _pareto(result["pallet"]),
    }
    return jsonsafe.clean(top)
