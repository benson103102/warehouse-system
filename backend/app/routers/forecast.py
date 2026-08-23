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


# 新品判定的預設視窗（個月），與前端「商品生命週期分類」的判斷視窗預設值一致。
# 單一來源放在 analytics：配置用的預測值（analytics.predicted_sku_values）現在也走同一套
# 新品判定與冷啟動，三處吃同一個預設值才不會各自漂移、出現「同一個 SKU 在這頁是新品、
# 在那頁不是」的矛盾。
COLD_START_RECENT_N = an.COLD_START_RECENT_N


def _fc_base(sess, granularity: str, horizon: int):
    """取得這一頁所有計算共用的基準：排除不完整末月後的期別、基準期切點、測試期別。

    三種預測區間（次一個月／次二個月／次一季）共用**同一個基準期切點**——切點固定用最長
    區間（3）往回推，選 1 或 2 時只是少驗幾期，站的位置不變，這樣三者才能互相比較。
    """
    agg = sess.cached(("fc_agg", granularity),
                      lambda: an.compute_forecast_agg(sess.cleaning_result.clean_df, granularity))
    if not agg:
        raise HTTPException(status_code=422, detail="清洗後無有效出貨資料可供預測")

    periods = an.forecast_periods(agg, granularity)   # 已排除尚未過完的最後一個月
    max_h = _test_len(granularity)
    if len(periods) < max_h + 4:
        raise HTTPException(
            status_code=422,
            detail=f"資料期數不足以做多步驗證（排除不完整末月後至少需 {max_h + 4} 期，"
                   f"目前僅 {len(periods)} 期）")
    train_end = an.forecast_base_idx(periods, max_h)
    return {
        "agg": agg, "periods": periods, "train_end": train_end, "horizon": horizon,
        "base_period": periods[train_end - 1] if train_end > 0 else None,
        "test_periods": periods[train_end:train_end + horizon],
        "dropped_period": (agg["periods"][-1]
                           if len(agg["periods"]) > len(periods) else None),
    }


def _cold_start_ctx(sess, ctx, granularity, target, recent_n):
    """組出「新品集合 + 類比水準對照表」，供 /series 與 /sku_top 判斷是否改用冷啟動預測。
    只跟 (granularity, target, recent_n) 有關、與查哪個 SKU／哪個區間無關，故做 session 快取。"""
    agg, train_end, periods = ctx["agg"], ctx["train_end"], ctx["periods"]

    def build():
        agg2 = dict(agg, periods=periods)   # 用排除不完整末月後的期別判定，與驗證基準一致
        new_set = an.new_sku_set(agg2, recent_n, train_end)
        sku_material = an.get_sku_material_map(sess.cleaning_result.clean_df)
        analogue = an.analogue_levels(agg2, sku_material, target, train_end, new_set)
        return {"new_set": new_set, "analogue": analogue, "sku_material": sku_material}

    return sess.cached(("fc_cold", granularity, target, recent_n), build)


def _analogue_level_for(ctx, agg, sku_id):
    meta = agg["sku_meta"].get(sku_id) or {}
    key = (meta.get("商品類別"), ctx["sku_material"].get(sku_id, "shelf"))
    return ctx["analogue"]["median"].get(key, 0.0)


def _sku_totals_in_periods(sess, ctx, granularity, target):
    """各 SKU 在「實際用於預測的期別」內的出貨合計（依 target 為出貨量或揀貨次數），
    由高到低排序。供 /sku_top 挑候選池與計算 SKU 總檔數。

    刻意不直接用 agg 裡現成的 sku_total／sku_total_cnt：那兩個是整份資料的累計，包含被
    forecast_periods() 排除掉的「尚未過完的最後一個月」。用它們來挑候選或當分母會有兩個
    問題——(1) 只在那個不完整末月出過貨的商品會被算進來，但它在預測看得到的期間內整條
    序列都是 0，預測不出任何東西；(2) 這個檔數會和「商品生命週期分類」對不起來，該頁同樣
    只認完整月份，於是同一畫面出現兩個不一樣的 SKU 總數（實測差 71 檔）。

    算法本身收在 analytics.sku_totals_in_periods()——ABC 分級頁的 predicted_sku_values() 也用
    同一支，兩邊的母體才不會各自漂移。這裡只多包一層 session 快取（拉門檻／換預測區間時不必
    重算），清洗結果一換即失效。"""

    def build():
        return an.sku_totals_in_periods(ctx["agg"], ctx["periods"], target)

    return sess.cached(("fc_sku_totals", granularity, target), build)


def _sku_prediction_rows(sess, ctx0, granularity, target, recent_n, horizon, skus):
    """對指定的一批 SKU 逐一做測試期驗證，回傳 (rows, 新品筆數)，依測試期實際合計由高到低排序。

    /sku_top（畫面上的 Top N 表）與 /sku_predictions（下載全部商品預測值）共用這一支，
    同一個 SKU 在兩邊算出來的預測值、誤差、選用模型才會一模一樣，不會因為兩處各寫一份而漂移。
    每列同時附上逐期的 pred／actual 陣列（長度＝horizon），下載時才能把各期預測值攤成欄位。"""
    agg, periods, train_end = ctx0["agg"], ctx0["periods"], ctx0["train_end"]
    key_map = agg["by_sku_cnt"] if target == "cnt" else agg["by_sku"]
    # 新品改用冷啟動類比預測、且不評分（同 /series 的處理）。週粒度沒有類比基礎資料，故只在月粒度套用。
    ctx = _cold_start_ctx(sess, ctx0, granularity, target, recent_n) if granularity == "month" else None

    rows = []
    new_count = 0
    for sku in skus:
        s = an.series_for(key_map, periods, key=sku)
        is_new = bool(ctx and sku in ctx["new_set"])
        if is_new:
            cs = an.cold_start_predict(s, horizon, _analogue_level_for(ctx, agg, sku), train_end)
            v = {"model": cs["model"], "pred": cs["pred"], "wape": None,
                 "actual": [s[train_end + h] if train_end + h < len(s) else 0.0
                            for h in range(horizon)]}
            new_count += 1
        else:
            v = an.fc_validate_multistep(s, horizon, train_end)
        meta = agg["sku_meta"].get(sku, {})
        actual_sum = sum(v["actual"])
        pred_sum = sum(v["pred"])
        # 新品的預測是冷啟動類比（水平線粗估），本來就不是要拿來評分的東西：
        # 逐期誤差(WAPE)與總量誤差一律不計算，避免用一個沒有意義的數字誤導判讀。
        tot_err = None if is_new else (
            (abs(pred_sum - actual_sum) / actual_sum * 100) if actual_sum > 0 else None)
        rows.append({
            "id": sku, "code": meta.get("商品編號"), "name": meta.get("商品名稱"),
            "cat": meta.get("商品類別"), "cat_name": an.category_label(meta.get("商品類別"), CATEGORY_NAME_BY_ID),
            "pred": v["pred"], "actual": v["actual"],
            "actual_sum": actual_sum, "pred_sum": pred_sum,
            "wape": v["wape"], "tot_err": tot_err, "model": v["model"],
            "is_new": is_new,
        })
    rows.sort(key=lambda r: -r["actual_sum"])
    return rows, new_count


@router.get("/series")
def series(granularity: str = Query("month", pattern="^(month|week)$"),
           target: str = Query("qty", pattern="^(qty|cnt)$"),
           sku_id: Optional[int] = None, cat_id: Optional[int] = None,
           recent_n: int = Query(COLD_START_RECENT_N, ge=1, le=24,
                                 description="新品判定視窗（最近 N 期），與生命週期分類同義"),
           horizon: int = Query(an.FC_TEST_MONTH, ge=1, le=an.FC_TEST_MONTH,
                                description="預測區間：1=次一個月／2=次二個月／3=次一季"),
           sess: state.SessionState = Depends(require_clean_result)):
    ctx = _fc_base(sess, granularity, horizon)
    agg, periods, train_end = ctx["agg"], ctx["periods"], ctx["train_end"]

    if sku_id is not None:
        key_map = agg["by_sku_cnt"] if target == "cnt" else agg["by_sku"]
        actual = an.series_for(key_map, periods, key=sku_id)
        meta = agg["sku_meta"].get(sku_id)
        label = {"id": sku_id, "name": meta.get("商品名稱") if meta else None,
                 "cat": meta.get("商品類別") if meta else None}
    elif cat_id is not None:
        key_map = agg["by_cat_cnt"] if target == "cnt" else agg["by_cat"]
        actual = an.series_for(key_map, periods, key=cat_id)
        label = {"cat": cat_id, "name": an.category_label(cat_id, CATEGORY_NAME_BY_ID)}
    else:
        key_map = agg["overall_cnt"] if target == "cnt" else agg["overall"]
        actual = an.series_for(key_map, periods)
        label = {"scope": "overall"}

    if all(v == 0 for v in actual):
        raise HTTPException(status_code=404, detail="查無出貨紀錄")

    # 新品（首次出貨落在基準期前 recent_n 期內）歷史太短，時序選模不可靠：
    # 改用冷啟動類比預測，且不回傳 MASE／WAPE（對應原型的「新品·冷啟動不評分」）。
    cold_info = None
    v = None
    if sku_id is not None and granularity == "month":
        cs_ctx = _cold_start_ctx(sess, ctx, granularity, target, recent_n)
        if sku_id in cs_ctx["new_set"]:
            lvl = _analogue_level_for(cs_ctx, agg, sku_id)
            cs = an.cold_start_predict(actual, horizon, lvl, train_end)
            v = {"model": cs["model"], "pred": cs["pred"], "mase": None, "wape": None}
            meta = agg["sku_meta"].get(sku_id) or {}
            key = (meta.get("商品類別"), cs_ctx["sku_material"].get(sku_id, "shelf"))
            cold_info = {
                "is_new": True, "recent_n": recent_n,
                "level": cs["level"], "level_source": cs["source"],
                "analogue_level": lvl,
                "peer_count": cs_ctx["analogue"]["peer_count"].get(key, 0),
                "material": cs_ctx["sku_material"].get(sku_id, "shelf"),
                "note": f"此商品判定為新品（首次出貨落在基準期前最近 {recent_n} 個月內），"
                        f"歷史太短、時序預測不可靠，改用類比估計；準確度指標不評分。",
            }
    if v is None:
        v = an.fc_validate_multistep(actual, horizon, train_end)

    return jsonsafe.clean({
        "label": label, "granularity": granularity, "target": target,
        "periods": periods, "actual": actual,
        "horizon": horizon, "test_len": horizon,
        "train_end": train_end, "base_period": ctx["base_period"],
        "test_periods": ctx["test_periods"], "dropped_period": ctx["dropped_period"],
        "model": v["model"], "pred_tail": v["pred"], "mase": v["mase"], "wape": v["wape"],
        "cold_start": cold_info,
    })


@router.get("/breakdown")
def breakdown(granularity: str = Query("month", pattern="^(month|week)$"),
              target: str = Query("qty", pattern="^(qty|cnt)$"),
              horizon: int = Query(an.FC_TEST_MONTH, ge=1, le=an.FC_TEST_MONTH,
                                   description="預測區間：1=次一個月／2=次二個月／3=次一季"),
              sess: state.SessionState = Depends(require_clean_result)):
    """各商品類別的多步驗證結果彙總表（對應原 forecastCatTable）。"""
    ctx = _fc_base(sess, granularity, horizon)
    agg, periods, train_end = ctx["agg"], ctx["periods"], ctx["train_end"]

    key_map = agg["by_cat_cnt"] if target == "cnt" else agg["by_cat"]
    rows = []
    for cat in agg["by_cat"].index.get_level_values(0).unique():
        s = an.series_for(key_map, periods, key=cat)
        v = an.fc_validate_multistep(s, horizon, train_end)
        rows.append({
            "cat": cat, "name": an.category_label(cat, CATEGORY_NAME_BY_ID),
            "actual_sum": sum(v["actual"]), "pred_sum": sum(v["pred"]),
            "wape": v["wape"], "mase": v["mase"], "model": v["model"],
        })
    rows.sort(key=lambda r: -r["actual_sum"])
    return jsonsafe.clean({
        "periods": periods, "horizon": horizon, "test_len": horizon,
        "base_period": ctx["base_period"], "test_periods": ctx["test_periods"],
        "rows": rows,
    })


@router.get("/sku_top")
def sku_top(granularity: str = Query("month", pattern="^(month|week)$"),
            target: str = Query("qty", pattern="^(qty|cnt)$"),
            candidate_n: int = Query(200, ge=1, le=1000),
            top_n: int = Query(20, ge=1, le=100),
            recent_n: int = Query(COLD_START_RECENT_N, ge=1, le=24,
                                  description="新品判定視窗（最近 N 期）"),
            horizon: int = Query(an.FC_TEST_MONTH, ge=1, le=an.FC_TEST_MONTH,
                                 description="預測區間：1=次一個月／2=次二個月／3=次一季"),
            sess: state.SessionState = Depends(require_clean_result)):
    """單一 SKU 測試期「預測 vs 實際」Top N（對應原 forecastSkuTable）。
    先取歷史出貨（量或次數）前 candidate_n 名 SKU 為候選池，各自做多步驗證，
    再依「測試期實際合計」由高到低取前 top_n 名回傳。

    候選池與回傳的 total_sku_count 都只認「實際用於預測的期別」內的出貨（已排除尚未過完
    的末月），與「商品生命週期分類」同一個母體，兩張卡片的 SKU 總檔數才會一致。"""
    ctx0 = _fc_base(sess, granularity, horizon)
    # 候選池與總檔數都以「實際用於預測的期別」為準（見 _sku_totals_in_periods）。
    totals = _sku_totals_in_periods(sess, ctx0, granularity, target)
    candidates = [idx for idx, _ in totals.head(candidate_n).items()]
    rows, new_count = _sku_prediction_rows(sess, ctx0, granularity, target, recent_n, horizon, candidates)
    return jsonsafe.clean({
        "periods": ctx0["periods"], "horizon": horizon, "test_len": horizon,
        "base_period": ctx0["base_period"], "test_periods": ctx0["test_periods"],
        "candidate_count": len(candidates), "total_sku_count": int(len(totals)),
        "new_sku_count": new_count, "recent_n": recent_n,
        "rows": rows[:top_n],
    })


@router.get("/sku_predictions")
def sku_predictions(granularity: str = Query("month", pattern="^(month|week)$"),
                    target: str = Query("qty", pattern="^(qty|cnt)$"),
                    recent_n: int = Query(COLD_START_RECENT_N, ge=1, le=24,
                                          description="新品判定視窗（最近 N 期）"),
                    horizon: int = Query(an.FC_TEST_MONTH, ge=1, le=an.FC_TEST_MONTH,
                                         description="預測區間：1=次一個月／2=次二個月／3=次一季"),
                    sess: state.SessionState = Depends(require_clean_result)):
    """全部商品的測試期預測值，供「需求趨勢及預測圖」的下載按鈕組成 CSV。

    與 /sku_top 共用 _sku_prediction_rows()，所以同一個 SKU 在畫面 Top N 表與下載檔裡看到的
    數字完全一致；差別只在這裡不做候選池截斷，涵蓋母體內每一個 SKU（Top N 表為了畫面可讀性
    才只顯示前幾名）。每個 SKU 都要跑一次選模＋多步驗證，數千檔時需要數秒——這是按下載才付出
    的成本，所以不併進畫面載入就會打的那些端點。"""
    ctx0 = _fc_base(sess, granularity, horizon)
    totals = _sku_totals_in_periods(sess, ctx0, granularity, target)
    rows, new_count = _sku_prediction_rows(sess, ctx0, granularity, target, recent_n, horizon,
                                            list(totals.index))
    return jsonsafe.clean({
        "granularity": granularity, "target": target, "horizon": horizon,
        "base_period": ctx0["base_period"], "test_periods": ctx0["test_periods"],
        "dropped_period": ctx0["dropped_period"], "recent_n": recent_n,
        "sku_count": len(rows), "new_sku_count": new_count,
        "rows": rows,
    })


@router.get("/lifecycle")
def lifecycle(granularity: str = Query("month", pattern="^(month|week)$"),
              recent_n: int = Query(COLD_START_RECENT_N, ge=1, le=24),
              sess: state.SessionState = Depends(require_clean_result)):
    ctx = _fc_base(sess, granularity, _test_len(granularity))
    # 期別與 train_end 都與 /series、/sku_top 用同一組（已排除不完整末月），
    # 三處的「新品」判定才會一致。
    agg2 = dict(ctx["agg"], periods=ctx["periods"])
    result = an.lifecycle_classify(agg2, recent_n, ctx["train_end"])
    result["dropped_period"] = ctx["dropped_period"]
    return jsonsafe.clean(result)
