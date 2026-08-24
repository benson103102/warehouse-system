"""
儲位配置端點 — 對應原前端 computeStorageAssignment()／whAllocateCategories()。
見 analytics.py 檔頭說明：分區資料表版本，取代原型的 SVG 像素座標切格。
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from .. import jsonsafe, state
from ..deps import require_clean_result
from ..services import analytics as an
from ..services.cleaning_core import CATEGORY_NAME_BY_ID

router = APIRouter(prefix="/api/storage", tags=["storage"])


# 新品判定視窗（個月），與需求預測頁「商品生命週期分類」預設值一致（單一來源在 analytics）。
COLD_START_RECENT_N = an.COLD_START_RECENT_N


def _cold_start_card(sess, sku_id, info, result):
    """組出「新品·冷啟動處理」卡片所需資料（對應原型 doProductQuery() 內的 newHtml 區塊）。

    新品歷史期數不足以跑時序選模，改用冷啟動估計熱度水準，並附上同類別＋同區成熟品的揀貨
    次數中位數當備貨參考。非新品回傳 None（前端就不顯示這張卡）。

    給位方式分兩種，直接讀 compute_wh_assignment() 實際算出來的 flex_slot_sku_ids，而不是
    在這裡自己重判一次——這張卡片講的處置必須跟地圖上的實際位置一致，否則會出現卡片寫著
    「不佔用黃金區」、地圖卻把它擺在黃金區的矛盾（這正是先前的問題）。
    """
    agg = sess.cached(("fc_agg", "month"),
                      lambda: an.compute_forecast_agg(sess.cleaning_result.clean_df, "month"))
    if not agg or not agg["periods"]:
        return None
    # 期別與基準期切點都比照需求預測頁（排除尚未過完的最後一個月），兩頁的新品判定才一致。
    periods = an.forecast_periods(agg, "month")
    train_end = an.forecast_base_idx(periods, an.FC_TEST_MONTH)
    if train_end < 1:
        return None
    recent_n = COLD_START_RECENT_N
    agg2 = dict(agg, periods=periods)
    new_set = sess.cached(("wh_new_set", recent_n),
                          lambda: an.new_sku_set(agg2, recent_n, train_end))
    if sku_id not in new_set:
        return None

    sku_material = sess.cached(("sku_material",),
                               lambda: an.get_sku_material_map(sess.cleaning_result.clean_df))
    material = sku_material.get(sku_id, "shelf")
    cat = info.get("cat")

    # 類比對象：同類別＋同儲位區的「成熟品」揀貨次數中位數（原型用 skuInfo.freq 的中位數）。
    freq = sess.cached(("freq",), lambda: an.sku_frequency(sess.cleaning_result.clean_df))
    peers = [it["freq"] for it in (freq["items"] if freq else [])
             if it["id"] != sku_id and it["id"] not in new_set
             and it["cat"] == cat and sku_material.get(it["id"], "shelf") == material]
    peers.sort()
    median_freq = peers[len(peers) // 2] if peers else None

    # 實際是否採彈性給位，以配置結果為準（見 analytics._predict_sku_value 的兩種新品）。
    flex = sku_id in set(result.get("flex_slot_sku_ids") or [])

    return {
        "is_new": True, "recent_n": recent_n, "flex_slot": flex,
        "material": material,
        "material_name": "棧板區" if material == "pallet" else "貨架區",
        "cat_name": an.category_label(cat, CATEGORY_NAME_BY_ID),
        "peer_count": len(peers), "median_freq": median_freq,
        "placement": (
            f"暫定彈性給位：訓練期內尚無自身出貨紀錄，熱度水準是拿同類別同區成熟品的中位數"
            f"推估的，沒有自身證據，因此不佔用黃金區，改依「類別群聚」暫放，待累積 "
            f"{recent_n} 個月後隨實際表現重排。"
            if flex else
            f"依預測值正常配位：歷史期數雖然不足以跑時序模型（判定為新品、改用冷啟動估計），"
            f"但訓練期內已有自身出貨紀錄，熱度水準取自實際觀測值而非猜測，因此照這個預測值"
            f"與其他商品一起競爭儲位，不強制排除於黃金區之外。"
        ),
        "stocking": "建議保守初次進貨＋較高安全庫存＋縮短補貨週期，由規劃人員覆核。",
    }


COPICK_LIFT_MIN = 1.3     # 與 compute_wh_assignment 內聚簇用的門檻一致
COPICK_MAX_ROWS = 10


def _copick_partners(sess, result, sku_id):
    """查詢商品的「共同揀取對應商品」清單（對應原型 doProductQuery() 的 copickHtml 區塊）。

    只列同區域（貨架/棧板）且 Lift ≥ 1.3 的組合——這正是 compute_wh_assignment() 拿來做
    聚簇的同一組條件，所以列出來的就是「改善後配置真的會想把它排在一起」的那些商品。
    每筆另附該關聯品的所屬區塊與熱度級別，並比對兩者在擺放順序中的位次差，回答
    「有沒有真的被排到相鄰」。
    """
    sku_co = result.get("sku_copick")
    if not sku_co:
        return {"rows": [], "lift_min": COPICK_LIFT_MIN}

    sku_material = sess.cached(("sku_material",),
                               lambda: an.get_sku_material_map(sess.cleaning_result.clean_df))
    my_mat = sku_material.get(sku_id, "shelf")
    my_zone = result["after_sku_zone"].get(sku_id)
    my_pos = result["after_sku_order"].get(sku_id)
    thresh = sku_co["lift_count_thresh"]

    rows = []
    for p in sku_co["pairs"]:
        other = None
        if p["a"]["id"] == sku_id:
            other = p["b"]
        elif p["b"]["id"] == sku_id:
            other = p["a"]
        if other is None or p["count"] < thresh or p["lift"] < COPICK_LIFT_MIN:
            continue
        oid = other["id"]
        o_mat = sku_material.get(oid, "shelf")
        if o_mat != my_mat:
            continue          # 不同區域本來就不可能相鄰，原型也是先濾掉
        o_zone = result["after_sku_zone"].get(oid)
        o_pos = result["after_sku_order"].get(oid)
        gap = abs(my_pos - o_pos) if (my_pos is not None and o_pos is not None) else None
        rows.append({
            "id": oid, "name": other.get("name"),
            "lift": p["lift"], "count": p["count"],
            "material": o_mat, "material_name": "棧板區" if o_mat == "pallet" else "貨架區",
            "zone": o_zone, "cls": result["sku_pred_cls"].get(oid) or other.get("cls"),
            "same_zone": (o_zone is not None and o_zone == my_zone),
            "order_gap": gap,
            # 位次差 1＝緊鄰；同區塊也算「已排在一起」（同區塊內即為相鄰儲位群）
            "adjacent": bool(gap == 1) if gap is not None else False,
        })
    rows.sort(key=lambda r: -r["lift"])
    rows = rows[:COPICK_MAX_ROWS]
    return {
        "rows": rows, "lift_min": COPICK_LIFT_MIN,
        "my_zone": my_zone, "my_material_name": "棧板區" if my_mat == "pallet" else "貨架區",
        "adjacent_count": sum(1 for r in rows if r["adjacent"]),
        "same_zone_count": sum(1 for r in rows if r["same_zone"]),
    }


@router.get("/wh_assignment")
def wh_assignment(a_thresh: float = Query(80, ge=0, le=100), b_thresh: float = Query(95, ge=0, le=100),
                   sku_id: Optional[int] = Query(None, description="只查單一 SKU 目前被分到哪個分區"),
                   sess: state.SessionState = Depends(require_clean_result)):
    """對應原前端 computeWhAssignment()／renderWhTabMap() 等：精細版儲位配置，依「共同揀取
    關聯」把商品分群、群內相鄰擺放（比 /assignment 的類別大鍋分配更細，見 analytics.py 第10節
    說明）。這一支比較貴（內含預測式ABC＋共同揀取分析），依 (a_thresh, b_thresh) 快取。"""
    result = sess.cached(("wh_assignment", a_thresh, b_thresh),
                         lambda: an.compute_wh_assignment(sess.cleaning_result.clean_df, CATEGORY_NAME_BY_ID,
                                                           sess.zones, a_thresh, b_thresh))
    if not result:
        raise HTTPException(status_code=422, detail="清洗後無有效出貨資料可供分析")

    if sku_id is not None:
        info = result["sku_info"].get(sku_id)
        if not info:
            raise HTTPException(status_code=404, detail="查無此商品ID的出貨紀錄")
        return jsonsafe.clean({
            "sku_id": sku_id, "info": info, "zone": result["after_sku_zone"].get(sku_id),
            "predicted_cls": result["sku_pred_cls"].get(sku_id),
            "cold_start": _cold_start_card(sess, sku_id, info, result),
            "copick": _copick_partners(sess, result, sku_id),
        })

    # 全量 after_sku_zone／sku_info 是「每個 SKU 一筆」，量體遠小於原始出貨明細（明細可能
    # 上百萬列，SKU 通常只有幾千個），一次回傳給前端自行畫地圖／查表即可，不需要再拆分頁。
    return jsonsafe.clean({
        "zone_before_cats": result["zone_before_cats"], "zone_after_class": result["zone_after_class"],
        "zone_items_class": result["zone_items_class"], "cell_class": result["cell_class"],
        "after_prop": result["after_prop"], "before_cat_zone": result["before_cat_zone"],
        "after_sku_zone": result["after_sku_zone"], "sku_count": len(result["sku_info"]),
        "a_thresh": a_thresh, "b_thresh": b_thresh,
    })


@router.get("/wh_assignment/rows")
def wh_assignment_rows(a_thresh: float = Query(80, ge=0, le=100), b_thresh: float = Query(95, ge=0, le=100),
                        sess: state.SessionState = Depends(require_clean_result)):
    """每個商品一列的「改善前 vs 改善後」儲位配置，供「商品配置查詢」的下載按鈕組成 CSV。

    與 /wh_assignment 共用同一把快取鍵，所以畫面已經載入過配置圖之後再按下載不會重算一次。
    刻意獨立成一支、而不是把這份明細掛進 /wh_assignment 的回應——它帶了商品名稱等文字欄位，
    量體是畫地圖那份的好幾倍，但只有按下載時才用得到；掛在主回應上會讓每次拉 A/B 門檻滑桿
    都白白多傳一次。

    等級欄位有兩個，含意不同、不能混用：
      predicted_cls  預測熱度級別（貨架依預測揀貨次數、棧板依預測出貨量，各自排名分級）
      place_cls      實際擺放等級——彈性給位的商品被暫放在 C 帶，就記 C。倉儲圖上的顏色是
                     照這個欄位上的，兩者不一致時以 place_cls 為準（見 analytics.place_after）。
    """
    result = sess.cached(("wh_assignment", a_thresh, b_thresh),
                         lambda: an.compute_wh_assignment(sess.cleaning_result.clean_df, CATEGORY_NAME_BY_ID,
                                                           sess.zones, a_thresh, b_thresh))
    if not result:
        raise HTTPException(status_code=422, detail="清洗後無有效出貨資料可供分析")

    zone_name = {z["id"]: z.get("name") for z in sess.zones}
    zone_material = {z["id"]: ("棧板區" if z.get("material") == "pallet" else "貨架區") for z in sess.zones}
    before_cat_zone = result["before_cat_zone"]
    after_sku_zone = result["after_sku_zone"]
    pred_cls = result["sku_pred_cls"]
    new_ids = set(result.get("new_sku_ids") or [])
    flex_ids = set(result.get("flex_slot_sku_ids") or [])

    rows = []
    for sku, info in result["sku_info"].items():
        cat = info.get("cat")
        bz = before_cat_zone.get(cat)
        az = after_sku_zone.get(sku)
        flex = sku in flex_ids
        pc = pred_cls.get(sku)
        rows.append({
            "id": sku, "name": info.get("name"),
            "cat": cat, "cat_name": an.category_label(cat, CATEGORY_NAME_BY_ID),
            "material": zone_material.get(az) or zone_material.get(bz),
            "before_zone": bz, "before_zone_name": zone_name.get(bz),
            "after_zone": az, "after_zone_name": zone_name.get(az),
            "predicted_cls": pc,
            "place_cls": ("C" if flex else pc),
            "pred_freq": info.get("freq"),
            "is_new": sku in new_ids, "flex_slot": flex,
        })
    # 依預測熱度由高到低，下載後打開就是「最該優先處理的商品在最上面」
    rows.sort(key=lambda r: -(r["pred_freq"] or 0))
    return jsonsafe.clean({"rows": rows, "sku_count": len(rows),
                            "a_thresh": a_thresh, "b_thresh": b_thresh})
