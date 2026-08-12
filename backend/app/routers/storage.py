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


@router.get("/zones")
def zones(sess: state.SessionState = Depends(require_clean_result)):
    """回傳目前 session 使用的儲位分區資料表（預設載入 zones_config.json）。"""
    return jsonsafe.clean({"zones": sess.zones})


@router.get("/assignment")
def assignment(a_thresh: float = Query(70, ge=0, le=100), b_thresh: float = Query(90, ge=0, le=100),
               sections: Optional[str] = Query(None, description="逗號分隔：upper,middle,lower"),
               materials: Optional[str] = Query(None, description="逗號分隔：shelf,pallet"),
               sess: state.SessionState = Depends(require_clean_result)):
    sec_set = set(sections.split(",")) if sections else None
    mat_set = set(materials.split(",")) if materials else None

    freq = sess.cached(("freq",), lambda: an.sku_frequency(sess.cleaning_result.clean_df))
    items = freq["items"] if freq else []
    # compute_storage_assignment 依賴門檻與分區/材質篩選（會變），故不快取；但它讀 items 是唯讀的，
    # 用共用的快取 freq 沒問題。真正貴的 sku_frequency 已被快取，這裡不會再重算一次。
    result = an.compute_storage_assignment(items, CATEGORY_NAME_BY_ID, sess.zones,
                                            a_thresh, b_thresh, sec_set, mat_set)

    # 回傳前把每個分類底下上百個「儲位格子」的原始清單去掉，只留彙總後的
    # zone_breakdown（分區代號→格數），避免payload過大；前端要畫地圖時直接
    # 用 /api/storage/zones 拿到的分區座標/距離資料表自行渲染即可。
    categories = []
    for c in result["categories"]:
        c2 = {k: v for k, v in c.items() if k != "cells"}
        categories.append(c2)

    return jsonsafe.clean({
        "categories": categories, "total_freq": result["total_freq"],
        "baseline_dist_m": result["baseline"], "weighted_dist_m": result["weighted"],
        "improvement_pct": result["improvement"], "coverage_pct": result["coverage"],
        "pool_size": result["pool_size"], "a_thresh": a_thresh, "b_thresh": b_thresh,
    })


@router.get("/wh_assignment")
def wh_assignment(a_thresh: float = Query(70, ge=0, le=100), b_thresh: float = Query(90, ge=0, le=100),
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
        })

    # 全量 after_sku_zone／sku_info 是「每個 SKU 一筆」，量體遠小於原始出貨明細（明細可能
    # 上百萬列，SKU 通常只有幾千個），一次回傳給前端自行畫地圖／查表即可，不需要再拆分頁。
    return jsonsafe.clean({
        "zone_before_cats": result["zone_before_cats"], "zone_after_class": result["zone_after_class"],
        "after_prop": result["after_prop"], "before_cat_zone": result["before_cat_zone"],
        "after_sku_zone": result["after_sku_zone"], "sku_count": len(result["sku_info"]),
        "a_thresh": a_thresh, "b_thresh": b_thresh,
    })
