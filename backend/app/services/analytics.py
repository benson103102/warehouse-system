"""
分析服務層 — 由前端原型（儲位配置與揀貨策略優化系統.html）中的 JS 運算函式移植而來。

對照表（原 JS 函式 → 本檔案函式）：
    computeSkuFreqData      → sku_frequency
    applyAbcThresholds      → apply_abc_thresholds
    computeCategoryAbcFromSku → category_abc_from_sku
    computeSkuCoPick        → sku_copick
    computeCategoryCoPick   → category_copick
    computeForecastAgg      → compute_forecast_agg
    holtForecast/fcNaive/...→ holt_forecast/fc_naive/...
    fcSelectBest/fcValidate → fc_select_best/fc_validate
    computeLifecycle        → lifecycle_classify
    getNewSkuSet            → new_sku_set
    ensureAnalogue/analogueLevel → analogue_levels
    fcColdStartPred         → cold_start_predict / fc_validate_cold
    computeBatch            → batch_simulate
    whAllocateCategories +
    computeStorageAssignment→ build_slot_pool + allocate_categories_to_slots + compute_storage_assignment
    runStorageSimulation    → compute_kpi

    ---- 2026-08 追加（第二批移植，見檔案後段「8～10」）----
    computePredictedCategoryAbc → compute_predicted_category_abc
    computePredictedSkuByZone → compute_predicted_sku_by_zone
    getCatMaterialMap/getSkuMaterialMap → get_cat_material_map / get_sku_material_map
    computeWhAssignment/whAllocateCategories(cwClusters版)→ compute_wh_assignment
    computeBatch(地理分群＋播種式批次)：
      batchParseGeo → 改用 cleaning_core 清洗階段已產出的「配送區域」欄位（縣市＋行政區
      合併字串），比原型的 batchParseGeo() 更完整（見 cleaning_core.py Part 18/21/22/23：
      地址回推、簡寫展開、稀疏地址排除等原型沒有的邏輯），故本檔只需 _split_city_district()
      把「配送區域」拆回縣市/行政區兩段，不需重新移植整張台灣縣市鄉鎮升格對照表。
      buildBatchOrders → build_batch_orders
      batchWindowMonths → batch_window_months
      batchBuildWaves   → batch_build_waves
      computeDayPlan    → compute_day_plan
      computeBatchStrategies → compute_batch_strategies
      batch_geo_context() 為新增的組裝函式：把 compute_wh_assignment() 算出的「SKU→儲位分區」
      結果轉成批次模擬要用的 zone_dist／sku_zone／before_zone，取代原型全域變數 WH_ASSIGN_CACHE。

輸入皆為 cleaning_core.run_cleaning_pipeline() 產生的乾淨資料 DataFrame（含出貨日期／
商品ID／商品編號／商品名稱／商品類別／系統出貨單號／訂購數量／配送區域／儲位分類 等
欄位），輸出為可直接序列化成 JSON 的 dict／list，供 API 路由回傳。

【與原型的一項刻意差異：儲位配置改為「分區資料表」而非「SVG 像素座標」】
原型的倉庫地圖是寫死的 SVG 像素座標（WH_ZONES 24 個區塊，再用程式碼切分成上百個
「儲位格子」畫在畫面上），這是「畫面呈現」的需求，不是「業務邏輯」——真實系統不會把
儲位資料庫存成像素座標，而是存「分區代號、坪數／可容納儲位數、到出貨口的距離」這種
資料表（可能來自 WMS 或人工現場量測）。本移植版本把 24 個真實分區的座標換算成
到出貨口的實際距離（見 zones_config.json，換算方式與原 whRouteDistance() 相同），
再依「面積 ÷ 每儲位假設坪效」估算可容納儲位數，取代原本的像素格子細分。
下游「依出貨次數分配類別到最近儲位」的演算法（最大餘數法）與原型完全相同。
"""

import math
from collections import defaultdict

from .cleaning_core import CURRENT_CITY_RE


# ============================================================
# 商品類別標籤
# ============================================================

def category_label(cat_id, cat_name_map):
    if cat_id is None:
        return "(空白)"
    try:
        if cat_id != cat_id:  # NaN
            return "(空白)"
    except TypeError:
        pass
    try:
        cid = int(cat_id)
    except (ValueError, TypeError):
        return str(cat_id)
    name = cat_name_map.get(cid)
    return f"{name}（{cid}）" if name else str(cid)


# ============================================================
# 1. SKU 出貨熱度（ABC 分級基礎資料）
# ============================================================

def _group_sets_by_key(key_arr, val_arr):
    """把等長的 key_arr／val_arr 依 key 分組成 {key: set(vals)}。

    原本這裡是 df.groupby(高基數字串欄).apply(lambda s: set(s.dropna()))：實測百萬列、
    十幾萬張訂單規模時非常慢——瓶頸不是「用 apply 呼叫 Python function」，而是 pandas
    對「高基數字串鍵」groupby 本身的雜湊/分組開銷就很大。改成 numpy 排序＋依邊界切段
    （對已排序好的陣列找出鍵值變動的位置，直接切片建 set，不經過 pandas 的 groupby
    機制），實測在 14 萬張訂單規模下約快 5 倍，且結果與原本逐組 apply 版本完全一致。"""
    import numpy as np
    if len(key_arr) == 0:
        return {}
    order = np.argsort(key_arr, kind="stable")
    ks = key_arr[order]
    vs = val_arr[order]
    boundaries = np.nonzero(ks[1:] != ks[:-1])[0] + 1
    starts = np.concatenate(([0], boundaries))
    ends = np.concatenate((boundaries, [len(ks)]))
    keys = ks[starts]
    return {k: set(vs[s:e]) for k, s, e in zip(keys, starts, ends)}


def sku_frequency(clean_df):
    """回傳 {items, order_items, total_orders}。
    items：依「出現在幾張不同訂單」由高到低排序的 SKU 清單（對應原 computeSkuFreqData）。
    order_items：{系統出貨單號: {商品ID,...}}，供 sku_copick() 共用揀取分析使用。"""
    df = clean_df.dropna(subset=["系統出貨單號", "商品ID"])
    if df.empty:
        return None
    order_items = _group_sets_by_key(df["系統出貨單號"].to_numpy(), df["商品ID"].to_numpy())

    item_freq = defaultdict(int)
    for ids in order_items.values():
        for iid in ids:
            item_freq[iid] += 1

    meta_df = df.drop_duplicates("商品ID").set_index("商品ID")
    # 一次性轉成字典（to_dict("index")），取代逐件商品 .loc[] 查詢——.loc[] 每次呼叫都有
    # 固定開銷，SKU 數上看幾千件時，逐件呼叫比一次轉字典再查詢慢得多。
    meta_lookup = meta_df[["商品編號", "商品名稱", "商品類別"]].to_dict("index")
    items = []
    for iid, freq in item_freq.items():
        row = meta_lookup.get(iid)
        items.append({
            "id": iid, "freq": freq,
            "code": (row["商品編號"] if row else ""),
            "name": (row["商品名稱"] if row else ""),
            "cat": (row["商品類別"] if row else None),
        })
    items.sort(key=lambda x: -x["freq"])
    return {"items": items, "order_items": order_items, "total_orders": len(order_items)}


def apply_abc_thresholds(items, a_thresh=70.0, b_thresh=90.0):
    """就地在 items 上加上 share／cum／cls，回傳門檻與總量（對應原 applyAbcThresholds）。"""
    total = sum(x["freq"] for x in items) or 1
    cum = 0.0
    for it in items:
        it["share"] = it["freq"] / total * 100
        cum += it["share"]
        it["cum"] = cum
        it["cls"] = "A" if cum <= a_thresh else ("B" if cum <= b_thresh else "C")
    return {"a_thresh": a_thresh, "b_thresh": b_thresh, "total": total}


def category_abc_from_sku(items, cat_name_map, a_thresh=70.0, b_thresh=90.0):
    """把已分級的 SKU 卷積成「商品類別」層級的 ABC 分級（對應原 computeCategoryAbcFromSku）。"""
    by_cat = {}
    for it in items:
        c = it["cat"]
        e = by_cat.setdefault(c, {"cat": c, "freq": 0, "sku_count": 0})
        e["freq"] += it["freq"]
        e["sku_count"] += 1
    arr = list(by_cat.values())
    for e in arr:
        e["name"] = category_label(e["cat"], cat_name_map)
    arr.sort(key=lambda x: -x["freq"])
    total = sum(x["freq"] for x in arr) or 1
    cum = 0.0
    for e in arr:
        e["share"] = e["freq"] / total * 100
        cum += e["share"]
        e["cum"] = cum
        e["cls"] = "A" if cum <= a_thresh else ("B" if cum <= b_thresh else "C")
    return arr


# ============================================================
# 2. 共同揀取（市場籃）分析
# ============================================================

def sku_copick(freq_result):
    """SKU 兩兩共同出現於同一張訂單的次數／support／lift（對應原 computeSkuCoPick）。"""
    items = freq_result["items"]
    order_items = freq_result["order_items"]
    id_to_idx = {it["id"]: i for i, it in enumerate(items)}
    n = len(items)
    pair_counts = defaultdict(int)
    for ids in order_items.values():
        if len(ids) < 2:
            continue
        idxs = sorted(id_to_idx[i] for i in ids if i in id_to_idx)
        for i in range(len(idxs)):
            for j in range(i + 1, len(idxs)):
                pair_counts[(idxs[i], idxs[j])] += 1

    total_orders = freq_result["total_orders"]
    count_thresh = 3 if total_orders < 3000 else (10 if total_orders < 20000 else 20)
    lift_count_thresh = 5 if total_orders < 3000 else (15 if total_orders < 20000 else 30)

    pairs = []
    for (ia, ib), cnt in pair_counts.items():
        if cnt < count_thresh:
            continue
        a, b = items[ia], items[ib]
        support = cnt / total_orders * 100
        lift = (cnt * total_orders) / (a["freq"] * b["freq"])
        pairs.append({"a": a, "b": b, "count": cnt, "support": support, "lift": lift})
    pairs.sort(key=lambda p: -p["count"])
    return {"pairs": pairs, "count_thresh": count_thresh,
            "lift_count_thresh": lift_count_thresh, "total_orders": total_orders}


def category_copick(clean_df, cat_name_map, cap_n=20):
    """商品類別 x 類別 共同出貨次數矩陣 + lift 矩陣（對應原 computeCategoryCoPick）。"""
    df = clean_df.dropna(subset=["系統出貨單號", "商品類別"])
    if df.empty:
        return None
    order_cats = _group_sets_by_key(df["系統出貨單號"].to_numpy(), df["商品類別"].to_numpy())

    freq = defaultdict(int)
    for cats in order_cats.values():
        for c in cats:
            freq[c] += 1
    top_cats = [c for c, _ in sorted(freq.items(), key=lambda kv: -kv[1])[:cap_n]]
    idx = {c: i for i, c in enumerate(top_cats)}
    n = len(top_cats)
    matrix = [[0] * n for _ in range(n)]
    for cats in order_cats.values():
        arr = [c for c in cats if c in idx]
        for i in range(len(arr)):
            for j in range(len(arr)):
                if i != j:
                    matrix[idx[arr[i]]][idx[arr[j]]] += 1

    total_orders = len(order_cats)
    lift_matrix = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            if i != j and matrix[i][j] > 0:
                lift_matrix[i][j] = (matrix[i][j] * total_orders) / (freq[top_cats[i]] * freq[top_cats[j]])

    max_v = max((v for row in matrix for v in row), default=0)
    max_lift = max((v for row in lift_matrix for v in row), default=0.0)
    labels = [category_label(c, cat_name_map) for c in top_cats]
    return {"labels": labels, "matrix": matrix, "max": max_v, "lift_matrix": lift_matrix,
            "max_lift": max_lift, "total_orders": total_orders, "cats": top_cats,
            "freqs": [freq[c] for c in top_cats]}


# ============================================================
# 3. 需求預測（多模型自動選模 + 最後一期 hold-out 驗證）
# ============================================================

def _week_key(ts):
    monday = ts - __import__("pandas").Timedelta(days=ts.weekday())
    return monday.strftime("%Y-%m-%d")


def _month_key(ts):
    return f"{ts.year:04d}-{ts.month:02d}"


def period_key(ts, granularity):
    return _month_key(ts) if granularity == "month" else _week_key(ts)


def _period_series(dates, granularity):
    """向量化版的 period_key：一次算完整欄的期別 key，取代逐列 apply。

    「出貨日期」欄是 object dtype 的 Timestamp 物件（由 .map 產生），不能直接用 .dt，
    所以先 pd.to_datetime 轉成 datetime64。關鍵：不要用 .dt.strftime（它其實是逐列
    格式化，百萬列比原本的 apply 還慢），而是先用向量化算出「期別代碼」，只對「不重複
    的期別」（通常幾十個）做字串格式化，再用 map 把整欄查表對回去——查表是 C 層雜湊，很快。
    輸出字串格式與純量版 _month_key／_week_key 完全一致（月："YYYY-MM"；週：該週週一 "YYYY-MM-DD"）。"""
    import pandas as pd
    dts = pd.to_datetime(dates)
    if granularity == "month":
        codes = dts.dt.year * 100 + dts.dt.month  # 向量化整數碼，例如 202407
        label = {int(c): f"{int(c) // 100:04d}-{int(c) % 100:02d}" for c in pd.unique(codes)}
        return codes.map(label)
    monday = (dts - pd.to_timedelta(dts.dt.weekday, unit="D")).dt.normalize()
    label = {pd.Timestamp(u): pd.Timestamp(u).strftime("%Y-%m-%d") for u in pd.unique(monday)}
    return monday.map(label)


def fill_periods(sorted_keys, granularity):
    """依最早～最晚出現的期別，補齊中間無出貨紀錄的期別（值為0），確保時間序列間距一致。"""
    import pandas as pd
    if not sorted_keys:
        return []
    first, last = sorted_keys[0], sorted_keys[-1]
    out = []
    if granularity == "month":
        y, m = int(first[:4]), int(first[5:7])
        ly, lm = int(last[:4]), int(last[5:7])
        while y < ly or (y == ly and m <= lm):
            out.append(f"{y:04d}-{m:02d}")
            m += 1
            if m > 12:
                m = 1
                y += 1
    else:
        t = pd.Timestamp(first)
        end_t = pd.Timestamp(last)
        while t <= end_t:
            out.append(t.strftime("%Y-%m-%d"))
            t += pd.Timedelta(days=7)
    return out


def compute_forecast_agg(clean_df, granularity):
    """把清洗後的訂單彙總成各期別的整體／各類別／各SKU出貨量與出貨次數序列
    （對應原 computeForecastAgg）。回傳的 by_cat／by_sku 為 pandas MultiIndex Series，
    供 series_for() 依 key 取出單一序列。"""
    df = clean_df.dropna(subset=["出貨日期", "訂購數量"]).copy()
    df = df[df["訂購數量"] > 0]
    if df.empty:
        return None
    df["period"] = _period_series(df["出貨日期"], granularity)

    overall = df.groupby("period")["訂購數量"].sum()
    overall_cnt = df.groupby("period").size()

    cat_df = df.dropna(subset=["商品類別"])
    by_cat = cat_df.groupby(["商品類別", "period"])["訂購數量"].sum()
    by_cat_cnt = cat_df.groupby(["商品類別", "period"]).size()

    sku_df = df.dropna(subset=["商品ID"])
    by_sku = sku_df.groupby(["商品ID", "period"])["訂購數量"].sum()
    by_sku_cnt = sku_df.groupby(["商品ID", "period"]).size()
    sku_total = sku_df.groupby("商品ID")["訂購數量"].sum()
    sku_total_cnt = sku_df.groupby("商品ID").size()
    sku_meta = sku_df.drop_duplicates("商品ID").set_index("商品ID")[["商品編號", "商品名稱", "商品類別"]].to_dict("index")

    periods = fill_periods(sorted(overall.index.tolist()), granularity)
    max_date = df["出貨日期"].max()
    return {
        "overall": overall, "overall_cnt": overall_cnt,
        "by_cat": by_cat, "by_cat_cnt": by_cat_cnt,
        "by_sku": by_sku, "by_sku_cnt": by_sku_cnt,
        "sku_total": sku_total, "sku_total_cnt": sku_total_cnt,
        "sku_meta": sku_meta, "periods": periods, "max_date": max_date,
    }


def series_for(indexed, periods, key=None):
    """把 compute_forecast_agg() 產生的 Series／MultiIndex-Series 對齊成固定期別陣列
    （對應原 forecastSeriesFor）。key=None 用於整體序列；有 key 時用於類別/SKU序列。"""
    if key is None:
        s = indexed
    else:
        try:
            s = indexed.xs(key, level=0)
        except KeyError:
            return [0.0] * len(periods)
    return [float(s.get(p, 0)) for p in periods]


def sku_totals_in_periods(agg, periods, target="cnt"):
    """各 SKU 在指定期別內的出貨合計（target="cnt" 揀貨次數／"qty" 出貨量），由高到低排序；
    合計為 0（該期間內完全沒出貨）者直接排除。

    存在的理由：agg 裡現成的 sku_total／sku_total_cnt 是「整份資料」的累計，包含被
    forecast_periods() 排除掉的、尚未過完的最後一個月。凡是以預測為主題的頁面，母體都該跟
    預測看得到的期間一致，否則只在那個末月出過貨的商品會被算進來——它在預測期間內整條序列
    都是 0，模型只會回 0，既沒有分級意義，也讓各頁的 SKU 總檔數互相對不起來（實測差 71 檔）。

    註：這類商品仍然是實際會出貨的商品，只是「還看不到」而非「需求為零」。它們不會因此從
    儲位配置消失——compute_wh_assignment 末段有保底，沒被 place_after 配到位的商品會落回
    所屬類別在「改善前」的分區。"""
    key_map = agg["by_sku_cnt"] if target == "cnt" else agg["by_sku"]
    in_window = key_map[key_map.index.get_level_values(1).isin(set(periods))]
    totals = in_window.groupby(level=0).sum()
    return totals[totals > 0].sort_values(ascending=False)


FC_TEST_MONTH = 3
FC_TEST_WEEK = 13
FC_HORIZONS = (1, 2, 3)   # 可選預測區間：次一個月／次二個月／次一季


def forecast_periods(agg, granularity="month"):
    """需求預測實際採用的期別：月粒度時排除「尚未過完」的最後一個月。

    例如資料抓到 2026-05-12 就停，5 月只累積了 12 天，把它當成一個完整月拿去驗證／
    當基準，會讓整體看起來突然大幅衰退（其實只是月份還沒過完）。排除後 2026-04 才是
    最後一個完整月。週粒度暫不處理（週的邊界判斷另有規則，且本專案 KPI 以月為準）。
    """
    if granularity != "month":
        return agg["periods"]
    return _predicted_periods(agg)


def forecast_base_idx(periods, max_horizon=FC_TEST_MONTH):
    """基準期切點（train_end）：固定用「最長預測區間」往回推算，讓 H=1/2/3 共用同一個基準期。

    例如最後一個完整月是 2026-04、max_horizon=3 → train_end 指向 2026-02，
    基準期＝2026-01，測試期自 2026-02 起算。選「次一個月」時就只驗 2026-02 這一期，
    但站的位置不變——三種區間才能互相比較（對應原 fcBaseIdx，但不寫死日期）。
    """
    return len(periods) - max_horizon


def holt_forecast(values, alpha, beta, horizon):
    n = len(values)
    if n == 0:
        return [0.0] * horizon
    if n == 1:
        return [float(values[0])] * horizon
    level = values[0]
    trend = values[1] - values[0]
    for t in range(1, n):
        val = values[t]
        nl = alpha * val + (1 - alpha) * (level + trend)
        nt = beta * (nl - level) + (1 - beta) * trend
        level, trend = nl, nt
    return [max(0.0, level + h * trend) for h in range(1, horizon + 1)]


def fc_naive(h):
    return h[-1] if h else 0.0


def fc_snaive(h):
    return h[-12] if len(h) >= 12 else (h[-1] if h else 0.0)


def fc_ma3(h):
    if not h:
        return 0.0
    k = min(3, len(h))
    return sum(h[-k:]) / k


def fc_ses(h, a=0.3):
    if not h:
        return 0.0
    level = h[0]
    for v in h[1:]:
        level = a * v + (1 - a) * level
    return level


def fc_holt(h):
    return holt_forecast(h, 0.3, 0.15, 1)[0]


def fc_croston(h, a=0.1):
    nz = [i for i, v in enumerate(h) if v > 0]
    if not nz:
        return 0.0
    z = h[nz[0]]
    x = nz[0] + 1
    last = nz[0]
    for j in nz[1:]:
        q = j - last
        z = a * h[j] + (1 - a) * z
        x = a * q + (1 - a) * x
        last = j
    return z / x if x > 0 else 0.0


FC_MODELS = [
    ("Naive", fc_naive), ("季節Naive", fc_snaive), ("MA3", fc_ma3),
    ("SES", fc_ses), ("Holt", fc_holt), ("Croston", fc_croston),
]


def fc_mean_abs_diff(a):
    if len(a) < 2:
        return 0.0
    return sum(abs(a[i] - a[i - 1]) for i in range(1, len(a))) / (len(a) - 1)


def fc_select_best(series, test_len):
    """訓練期內 walk-forward（擴張窗，h=1）依 MASE 自動選最佳模型（對應原 fcSelectBest）。"""
    n = len(series)
    train_end = n - test_len
    train = series[:train_end]
    denom = fc_mean_abs_diff(train)
    start = max(3, train_end - 6)
    best_name, best_fn, best_mase = FC_MODELS[0][0], FC_MODELS[0][1], float("inf")
    for name, fn in FC_MODELS:
        ae, cnt = 0.0, 0
        for t in range(start, train_end):
            ae += abs(series[t] - fn(series[:t]))
            cnt += 1
        if cnt > 0:
            mase = (ae / cnt) / denom if denom > 0 else (ae / cnt)
        else:
            mase = float("inf")
        if mase < best_mase:
            best_mase, best_name, best_fn = mase, name, fn
    return {"model_name": best_name, "model_fn": best_fn, "denom": denom,
            "train_end": train_end, "train_mase": best_mase}


def fc_validate(series, test_len):
    """用選定模型對最後 test_len 期滾動一步預測，回傳預測/實際/準確度（對應原 fcValidate）。"""
    sel = fc_select_best(series, test_len)
    n = len(series)
    pred, actual = [], []
    for t in range(n - test_len, n):
        pred.append(max(0.0, sel["model_fn"](series[:t])))
        actual.append(series[t])
    ae = sum(abs(p - a) for p, a in zip(pred, actual))
    sum_a = sum(actual)
    mase = (ae / test_len) / sel["denom"] if sel["denom"] > 0 else None
    wape = (ae / sum_a * 100) if sum_a > 0 else None
    return {"model": sel["model_name"], "pred": pred, "actual": actual, "mase": mase, "wape": wape}


# ---- 多步驗證（對應原 fcSelectBestAt／fcMultiStep／fcValidateMS）----------------
#
# 與上面 fc_validate（滾動一步）的差別，是這次改用的驗證方式的重點：
#   滾動一步：預測第 t 期時，可以看到第 t-1 期的**實際值**。等於每走一步就被餵一次答案，
#            只回答得了「下個月大概多少」，而且會把準確度估得偏樂觀。
#   多步    ：站在基準期（train_end）一次往後推 H 期，過程中完全看不到測試期任何實際值。
#            這才對應真實情境——採購在 1 月底要一次決定 2、3、4 月各備多少貨。
# 所以「次一個月／次二個月／次一季」三種預測區間共用**同一個基準期**，只是往後推得遠近不同。

def fc_select_best_at(series, train_end):
    """在指定的基準期切點上做 walk-forward 選模（對應原 fcSelectBestAt）。
    與 fc_select_best 的差別只在 train_end 由呼叫端指定，而不是用 len-test_len 反推——
    這樣不同預測區間（H=1/2/3）可以固定用同一個基準期選模。"""
    train = series[:train_end]
    denom = fc_mean_abs_diff(train)
    start = max(3, train_end - 6)
    best_name, best_fn, best_mase = FC_MODELS[0][0], FC_MODELS[0][1], float("inf")
    for name, fn in FC_MODELS:
        ae, cnt = 0.0, 0
        for t in range(start, train_end):
            ae += abs(series[t] - fn(series[:t]))
            cnt += 1
        mase = ((ae / cnt) / denom if denom > 0 else (ae / cnt)) if cnt > 0 else float("inf")
        if mase < best_mase:
            best_mase, best_name, best_fn = mase, name, fn
    return {"model_name": best_name, "model_fn": best_fn, "denom": denom,
            "train_end": train_end, "train_mase": best_mase}


def fc_multi_step(name, series, train_end, h):
    """用選定模型從 train_end 往後預測第 h 期（對應原 fcMultiStep）。
    只餵 series[:train_end]，絕不讓模型看到測試期的實際值。"""
    hist = series[:train_end]
    if name == "Holt":
        return holt_forecast(hist, 0.3, 0.15, h)[h - 1]
    if name == "季節Naive":
        j = train_end + h - 1
        return series[j - 12] if j - 12 >= 0 else (hist[-1] if hist else 0.0)
    # 其餘模型（Naive／MA3／SES／Croston）本身沒有趨勢項，多步預測即水平延伸，
    # 每一期都回同一個值——與原型行為一致。
    fn = dict(FC_MODELS).get(name, fc_naive)
    return fn(hist)


def fc_validate_multistep(series, horizon, train_end):
    """多步驗證：站在 train_end 一次往後推 horizon 期，與實際值比對（對應原 fcValidateMS）。"""
    sel = fc_select_best_at(series, train_end)
    name = sel["model_name"]
    pred, actual = [], []
    for h in range(1, horizon + 1):
        j = train_end + h - 1
        pred.append(max(0.0, fc_multi_step(name, series, train_end, h)))
        actual.append(series[j] if j < len(series) else 0.0)
    ae = sum(abs(p - a) for p, a in zip(pred, actual))
    sum_a = sum(actual)
    mase = (ae / horizon) / sel["denom"] if sel["denom"] > 0 else None
    wape = (ae / sum_a * 100) if sum_a > 0 else None
    return {"model": name, "pred": pred, "actual": actual, "mase": mase, "wape": wape}


# ============================================================
# 4. 商品生命週期分類（新品／滯銷／正常）
# ============================================================

def lifecycle_classify(agg, recent_n, train_end=None):
    """新品／滯銷／正常分類。

    新品的判準與需求預測的冷啟動一致：一律站在「做預測的時間點」（train_end，即訓練期
    結束、測試期開始的切點）回頭看 recent_n 期。這樣同一個 SKU 在生命週期卡、預測趨勢圖、
    SKU Top 表三處的「是不是新品」不會互相矛盾（見 new_sku_set）。
    train_end=None 時預設為 n - FC_TEST_MONTH，與 fc_validate 的切點相同。
    滯銷仍以「資料結尾」為準——它問的是「到現在為止還有沒有在動」，本來就該看最新狀態。
    """
    periods = agg["periods"]
    n = len(periods)
    if train_end is None:
        train_end = n - FC_TEST_MONTH
    rows = []
    for sku, meta in agg["sku_meta"].items():
        vals = series_for(agg["by_sku"], periods, key=sku)
        first = last = -1
        nz = 0
        for i, v in enumerate(vals):
            if v > 0:
                if first < 0:
                    first = i
                last = i
                nz += 1
        if first < 0:
            continue
        dorm_months = n - 1 - last
        cls = "新品" if first >= train_end - recent_n else ("滯銷" if last <= n - 1 - recent_n else "正常")
        rows.append({
            "sku": sku, "code": meta.get("商品編號"), "name": meta.get("商品名稱"),
            "cat": meta.get("商品類別"), "first": periods[first], "last": periods[last],
            "nz": nz, "dorm_months": dorm_months, "cls": cls,
        })
    counts = {"新品": 0, "滯銷": 0, "正常": 0}
    for r in rows:
        counts[r["cls"]] += 1
    return {"rows": rows, "counts": counts, "periods": periods, "recent_n": recent_n,
            "train_end": train_end,
            "base_period": periods[train_end - 1] if 0 < train_end <= n else None}


# ============================================================
# 4b. 新品·冷啟動處理（對應原 getNewSkuSet／ensureAnalogue／analogueLevel／fcColdStartPred）
#
# 為什麼要另外處理：新品的歷史序列太短（首次出貨才落在最近 N 個月），fc_select_best()
# 的 walk-forward 選模其實是在對一串「幾乎全是 0、只有末尾幾期有值」的序列挑模型，
# 選出來的模型與算出來的 MASE／WAPE 都沒有統計意義。原型的做法是改用「類比估計」：
# 拿同類別＋同儲位區（貨架/棧板）的**成熟品**水準中位數當新品的預測值，並明確標示
# 「新品·冷啟動不評分」——不給 MASE／WAPE，避免用一個不可靠的數字誤導使用者。
# ============================================================

COLD_START_MODEL = "冷啟動(類比)"
# cold_start_predict() 的 source 值：水準取自「自身近期實際值」代表這個新品在訓練期內
# 確實有出貨紀錄（只是期數不足以跑時序選模），與「完全沒有自身資料、拿同類中位數猜」
# 是兩種可信度截然不同的情況，儲位配置據此決定要不要彈性給位（見 _predict_sku_value）。
COLD_START_SOURCE_OWN = "自身近期實際值"

# 新品判定的預設視窗（個月）。需求預測頁（routers/forecast.py）、儲位配置的商品查詢卡片
# （routers/storage.py）、以及配置用的預測值（predicted_sku_values）三處共用這一個預設值，
# 才不會出現「同一個 SKU 在 A 頁被判為新品、在 B 頁不是」的矛盾（見 new_sku_set）。
#
# 為什麼是 3 不是 6：新品會走冷啟動預測、且在儲位配置裡採「暫定彈性給位」不佔黃金區
# （見 place_after），視窗開太大會把已經有半年以上穩定出貨、其實跑得動時序模型的商品也
# 一起掃進來。實測正式資料（24 個月、5,643 個 SKU）：
#     recent_n=6 → 新品 3,387 個（60.0%）
#     recent_n=3 → 新品   796 個（14.1%）
# 差距這麼大是因為該份資料在 2025-08～10 有一波約 2,600 個 SKU 的集中上架（平常每月僅
# 60～90 個），視窗 6 個月剛好整個罩進去。3 個月剛好落在那波之後，讓那批商品以「正常品」
# 身分用自己的歷史跑預測，只有真正剛上架、歷史確實不足的商品才走冷啟動。
COLD_START_RECENT_N = 3


def new_sku_set(agg, recent_n, train_end=None):
    """回傳「新品」SKU 集合：首次出貨落在訓練期結束前 recent_n 期之內（對應原 getNewSkuSet）。

    train_end 預設為 len(periods) - FC_TEST_MONTH，即與 fc_validate 的訓練/測試切點一致：
    判斷是不是新品要站在「做預測的那個時間點」上看，不能偷看測試期。
    """
    periods = agg["periods"]
    n = len(periods)
    if not n:
        return set()
    if train_end is None:
        train_end = n - FC_TEST_MONTH
    out = set()
    for sku in agg["sku_meta"]:
        vals = series_for(agg["by_sku"], periods, key=sku)
        first = next((i for i, v in enumerate(vals) if v > 0), -1)
        if first >= 0 and first >= train_end - recent_n:
            out.add(sku)
    return out


def analogue_levels(agg, sku_material, target, train_end, new_set):
    """建立「(類別, 儲位區) → 成熟品平均每期水準的中位數」對照表（對應原 ensureAnalogue）。

    只用非新品（成熟品）在訓練期內的資料算，避免拿新品去估新品。
    """
    periods = agg["periods"]
    key_map = agg["by_sku_cnt"] if target == "cnt" else agg["by_sku"]
    groups = {}
    for sku, meta in agg["sku_meta"].items():
        if sku in new_set:
            continue
        vals = series_for(key_map, periods, key=sku)
        total = sum(vals[:train_end])
        if total <= 0:
            continue
        level = total / train_end if train_end > 0 else 0.0
        key = (meta.get("商品類別"), sku_material.get(sku, "shelf"))
        groups.setdefault(key, []).append(level)
    med = {}
    for key, arr in groups.items():
        arr.sort()
        med[key] = arr[len(arr) // 2]
    return {"median": med, "peer_count": {k: len(v) for k, v in groups.items()}}


def cold_start_predict(series, horizon, analogue_level=0.0, train_end=None):
    """新品的冷啟動預測（對應原 fcColdStartPred）：優先用自身「訓練期內最後一個非零值」，
    完全沒有自身實際值時才退回同類別／同區的類比水準。預測為水平線（每期同值）。"""
    if train_end is None:
        train_end = len(series)
    level = 0.0
    for i in range(min(train_end, len(series)) - 1, -1, -1):
        if series[i] > 0:
            level = series[i]
            break
    source = COLD_START_SOURCE_OWN
    if level <= 0:
        level = analogue_level or 0.0
        source = "同類別同區成熟品中位數"
    level = max(0.0, float(level))
    return {"model": COLD_START_MODEL, "pred": [level] * horizon,
            "level": level, "source": source, "cold": True}


def fc_validate_cold(series, test_len, analogue_level=0.0):
    """新品版的 fc_validate：結構與 fc_validate 一致，但預測改用冷啟動，且**不給準確度指標**
    （mase／wape 皆為 None），對應原型的「新品·冷啟動不評分」。"""
    n = len(series)
    train_end = n - test_len
    cs = cold_start_predict(series, test_len, analogue_level, train_end)
    actual = [series[t] for t in range(train_end, n)]
    return {"model": cs["model"], "pred": cs["pred"], "actual": actual,
            "mase": None, "wape": None, "cold": True,
            "level": cs["level"], "level_source": cs["source"]}


# ============================================================
# 5. 批次併單模擬
# ============================================================

def batch_simulate(clean_df, capacity):
    lines = clean_df.dropna(subset=["系統出貨單號"]).groupby("系統出貨單號").size()
    order_count = int(len(lines))
    total_lines = int(lines.sum())
    single_rounds = order_count
    batch_rounds = max(1, math.ceil(total_lines / capacity)) if capacity > 0 else single_rounds
    improvement = (single_rounds - batch_rounds) / single_rounds * 100 if single_rounds > 0 else 0.0
    return {"order_count": order_count, "total_lines": total_lines, "single_rounds": single_rounds,
            "batch_rounds": batch_rounds, "improvement": improvement, "capacity": capacity}


# ============================================================
# 6. 儲位配置（分區資料表版本，見檔案頂端說明）
# ============================================================

UNIT_SLOT_AREA_M2 = 2.0  # 假設每個「儲位」約需 2 平方公尺（可依實際貨架/棧板規格調整）


def build_slot_pool(zones, sections=None, materials=None):
    sections = sections or {"upper", "middle", "lower"}
    materials = materials or {"shelf", "pallet"}
    pool = []
    for z in zones:
        if z["section"] not in sections or z["material"] not in materials:
            continue
        slot_count = max(1, round(z["area_m2"] / UNIT_SLOT_AREA_M2))
        for i in range(slot_count):
            pool.append({
                "id": f"{z['id']}-{i}", "zone_id": z["id"], "zone_name": z["name"],
                "section": z["section"], "material": z["material"], "dist": z["distance_m"],
            })
    pool.sort(key=lambda c: c["dist"])
    return pool


def allocate_categories_to_slots(categories, pool):
    """依「最大餘數法」把儲位依出貨次數比例分給類別，從最近的儲位開始依序給出貨次數
    最高的類別（對應原 whAllocateCategories）。就地修改 categories，附加 cell_count／
    cells／avg_dist／zone_breakdown。"""
    n = len(pool)
    total_freq = sum(c["freq"] for c in categories) or 1
    raw = [n * (c["freq"] / total_freq) for c in categories]
    base = [math.floor(v) for v in raw]
    used = sum(base)
    remainder = n - used
    order = sorted(range(len(categories)), key=lambda i: -(raw[i] - base[i]))
    for k in range(min(remainder, len(order))):
        base[order[k]] += 1

    cursor = 0
    for c, cnt in zip(categories, base):
        c["cell_count"] = cnt
        c["cells"] = pool[cursor:cursor + cnt]
        cursor += cnt
        if cnt:
            ds = [x["dist"] for x in c["cells"]]
            c["avg_dist"] = sum(ds) / len(ds)
            c["min_dist"] = min(ds)
            c["max_dist"] = max(ds)
            zb = defaultdict(int)
            for x in c["cells"]:
                zb[x["zone_name"]] += 1
            c["zone_breakdown"] = list(zb.items())
        else:
            c["avg_dist"] = None
            c["min_dist"] = None
            c["max_dist"] = None
            c["zone_breakdown"] = []
    return categories


def compute_storage_assignment(items, cat_name_map, zones, a_thresh=70.0, b_thresh=90.0,
                                sections=None, materials=None):
    """對應原 computeStorageAssignment：依「儲位配置」頁門檻分級的類別 ABC，把最近的儲位
    優先分給出貨次數最高的類別，回傳基準（未規劃）平均距離 vs 改善後加權平均距離。"""
    pool = build_slot_pool(zones, sections, materials)
    baseline = (sum(c["dist"] for c in pool) / len(pool)) if pool else 0.0
    if not items or not pool:
        return {"categories": [], "total_freq": 0, "baseline": baseline, "weighted": baseline,
                "improvement": 0.0, "coverage": 0.0, "pool_size": len(pool)}

    categories = category_abc_from_sku(items, cat_name_map, a_thresh, b_thresh)
    total_freq = sum(c["freq"] for c in categories) or 1
    allocate_categories_to_slots(categories, pool)

    placed = [c for c in categories if c["cell_count"] > 0]
    covered_freq = sum(c["freq"] for c in placed)
    weighted = (sum(c["freq"] * c["avg_dist"] for c in placed) / covered_freq) if covered_freq else baseline
    improvement = (baseline - weighted) / baseline * 100 if baseline else 0.0

    return {"categories": categories, "total_freq": total_freq, "baseline": baseline,
            "weighted": weighted, "improvement": improvement,
            "coverage": (covered_freq / total_freq * 100) if total_freq else 0.0,
            "pool_size": len(pool)}


# ============================================================
# 7. KPI（距離／揀貨回合數／揀貨工時改善率）
# ============================================================

def compute_kpi(strategies, speed=1.0, handle_sec=12.0):
    """對應原型『KPI 儀表板』頁 renderKpiScreen()：整合『揀貨策略』頁同一套地理感知批次
    模擬結果（compute_batch_strategies：逐單揀貨 vs 播種式批次揀貨），算出移動距離／揀貨
    回合數／揀貨工時改善率，並與專案章程訂下的 KPI 目標（距離≥10%、回合數≥10%、工時≥5%）
    比對。

    改版重點（沿用原型算法，逐項對齊）：
    - 距離／回合數改善率只跟儲位配置與批次分波有關，跟步行速度／取放秒數無關：直接沿用
      strategies 內「single_before」(改善前儲位＋逐單，每張訂單各自來回一次) 與「seed」
      (改善後儲位＋播種式批次，同一波內合併回合) 這組本來就存在、且與『揀貨策略』頁顯示
      數字一致的真實路徑距離／回合數。先前改用「儲位表全體平均距離」這種跟實際揀貨路徑
      無關的粗略估計值，在儲位/類別數量夠多時兩種平均會互相收斂，改善率被拉到接近 0%
      （使用者回報的 0.2% 即為此故）。
    - 工時改善率才跟步行速度／取放秒數有關，且比照原型分別計算：改善前（逐單）取放次數
      用「總明細行數」total_lines_raw（每張訂單各自取放，即使同商品也各取一次）；改善後
      （播種式批次）取放次數用「播種後實際揀取品項數」seed_picks（同一波內同商品合併只
      取一次，本來就會比改善前少，是批次揀貨除了縮短距離外的另一項時間節省來源）；以
      「小時」為單位（原型即以 /3600 表示），不額外乘以人力／AGV 因子——那是原型另一個
      「儲位／情境模擬」頁的參數，實際保留下來的 KPI 儀表板頁面本身沒有這項設定。"""
    orig_dist = strategies["single_before"]["dist"]
    opt_dist = strategies["seed"]["dist"]
    dist_improvement = (orig_dist - opt_dist) / orig_dist * 100 if orig_dist else 0.0

    orig_rounds = strategies["single"]["rounds"]
    opt_rounds = strategies["seed"]["rounds"]
    rounds_improvement = strategies["seed"]["imp_rounds"]

    orig_picks = strategies["total_lines_raw"]
    opt_picks = strategies["seed_picks"]

    orig_worktime = (orig_dist / speed + orig_picks * handle_sec) / 3600.0
    opt_worktime = (opt_dist / speed + opt_picks * handle_sec) / 3600.0
    worktime_improvement = (orig_worktime - opt_worktime) / orig_worktime * 100 if orig_worktime else 0.0

    return {
        "speed": speed, "handle_sec": handle_sec,
        "orig_dist": orig_dist, "opt_dist": opt_dist, "dist_improvement": dist_improvement,
        "orig_worktime": orig_worktime, "opt_worktime": opt_worktime,
        "worktime_improvement": worktime_improvement,
        "single_rounds": orig_rounds, "batch_rounds": opt_rounds,
        "rounds_improvement": rounds_improvement, "capacity": strategies["capacity"],
        "targets": {"dist": 10.0, "rounds": 10.0, "worktime": 5.0},
        "targets_met": {
            "dist": dist_improvement >= 10.0,
            "rounds": rounds_improvement >= 10.0,
            "worktime": worktime_improvement >= 5.0,
        },
    }


# ============================================================
# 8. 材質分類（貨架／棧板）——供預測式 ABC 與儲位配置分區使用
# ============================================================

def get_cat_material_map(clean_df):
    """商品類別 → 'shelf'／'pallet'（類別內多數商品的「儲位分類」決定，對應原 getCatMaterialMap）。"""
    df = clean_df.dropna(subset=["商品類別"])
    if df.empty or "儲位分類" not in df.columns:
        return {}
    tally = df.groupby("商品類別")["儲位分類"].apply(lambda s: (s == "棧板").sum() > (s == "貨架").sum())
    return {cat: ("pallet" if is_pallet else "shelf") for cat, is_pallet in tally.items()}


def get_sku_material_map(clean_df):
    """商品ID → 'shelf'／'pallet'（對應原 getSkuMaterialMap）。"""
    df = clean_df.dropna(subset=["商品ID"])
    if df.empty or "儲位分類" not in df.columns:
        return {}
    m = df.drop_duplicates("商品ID").set_index("商品ID")["儲位分類"]
    return {sku: ("pallet" if v == "棧板" else "shelf") for sku, v in m.items()}


# ============================================================
# 9. 預測式 ABC 分級（用預測值而非歷史值分級，對應原 computePredictedCategoryAbc／
#    computePredictedSkuByZone）
# ============================================================

def _predicted_periods(agg):
    """若最後一期（月）尚未過完（出貨資料的最大日期還沒到那個月的月底），排除該不完整月，
    避免用「還在累積中」的當月資料做驗證／預測基礎（對應原兩個函式開頭共用的邏輯）。"""
    import calendar
    import pandas as pd
    periods = agg["periods"]
    max_date = agg.get("max_date")
    if max_date is not None and periods:
        last = periods[-1]
        y, mo = int(last[:4]), int(last[5:7])
        last_day = calendar.monthrange(y, mo)[1]
        ld = pd.Timestamp(year=y, month=mo, day=last_day)
        if max_date < ld:
            periods = periods[:-1]
    return periods


def compute_predicted_category_abc(clean_df, cat_name_map, a_thresh=70.0, b_thresh=90.0):
    """對應原 computePredictedCategoryAbc：貨架類商品依「預測揀貨次數」分級、棧板類依
    「預測出貨量」分級（因為貨架區重效率／棧板區重量體，兩種儲位在意的指標不同）。"""
    agg = compute_forecast_agg(clean_df, "month")
    if not agg or not agg["periods"]:
        return None
    periods = forecast_periods(agg, "month")
    horizon = FC_TEST_MONTH
    if len(periods) < horizon + 4:
        return {"insufficient": True}
    # 基準期切點與需求預測頁 /api/forecast/breakdown 取同一個（forecast_base_idx），且同樣
    # 改用「多步驗證」——站在基準期一次往後推 horizon 期，過程中看不到測試期任何實際值。
    # 先前用的 fc_validate() 是滾動一步預測，每走一步都會被餵前一期的實際值，會把準確度
    # 估得偏樂觀（見 fc_validate_multistep 上方的說明），與需求預測頁顯示的數字對不起來。
    train_end = forecast_base_idx(periods, horizon)

    cat_mat = get_cat_material_map(clean_df)
    cats = set(agg["by_cat"].index.get_level_values(0).unique()) | set(agg["by_cat_cnt"].index.get_level_values(0).unique())
    rows = []
    for c in cats:
        sc = series_for(agg["by_cat_cnt"], periods, key=c)
        sq = series_for(agg["by_cat"], periods, key=c)
        # 註：原本這裡有一行 `if len(sc) < test_len + 4: continue`。series_for() 一定回傳
        # 長度等於 len(periods) 的陣列（該期沒出貨就補 0），而上面已經擋掉
        # len(periods) < horizon + 4，所以該條件恆為 False，是永遠不會執行的死碼，已移除。
        vc = fc_validate_multistep(sc, horizon, train_end)
        vq = fc_validate_multistep(sq, horizon, train_end)
        rows.append({
            "cat": c, "name": category_label(c, cat_name_map), "material": cat_mat.get(c, "shelf"),
            "pred_cnt": sum(vc["pred"]), "pred_qty": sum(vq["pred"]),
        })

    def pareto(lst, key):
        arr = sorted(lst, key=lambda x: -x[key])
        total = sum(x[key] for x in arr) or 1
        cum = 0.0
        out = []
        for x in arr:
            x = dict(x)
            x["metric"] = x[key]
            x["share"] = x[key] / total * 100
            cum += x["share"]
            x["cum"] = cum
            x["cls"] = "A" if cum <= a_thresh else ("B" if cum <= b_thresh else "C")
            out.append(x)
        return out

    shelf = pareto([r for r in rows if r["material"] == "shelf"], "pred_cnt")
    pallet = pareto([r for r in rows if r["material"] == "pallet"], "pred_qty")
    return {"shelf": shelf, "pallet": pallet, "a_thresh": a_thresh, "b_thresh": b_thresh, "periods": periods}


def _forecast_ctx(clean_df, recent_n=COLD_START_RECENT_N):
    """配置用預測值的共同基準：期別（已排除尚未過完的末月）、基準期切點、預測區間、新品集合。

    這四樣刻意跟需求預測頁（routers/forecast.py 的 _fc_base／_cold_start_ctx）取同一套函式
    ——forecast_periods()／forecast_base_idx()／FC_TEST_MONTH／new_sku_set()——這樣「需求預測頁
    上顯示的預測值」與「儲位配置拿來做 ABC 分級的預測值」才會是同一個東西，不會同一個 SKU
    在兩頁看到兩個數字。資料期數不足以做多步驗證時回傳 None（呼叫端轉成 insufficient）。"""
    agg = compute_forecast_agg(clean_df, "month")
    if not agg or not agg["periods"]:
        return None
    periods = forecast_periods(agg, "month")
    horizon = FC_TEST_MONTH
    if len(periods) < horizon + 4:
        return None
    train_end = forecast_base_idx(periods, horizon)
    agg2 = dict(agg, periods=periods)   # 新品判定要用「排除末月後」的期別，與驗證基準一致
    return {
        "agg": agg, "agg2": agg2, "periods": periods, "horizon": horizon,
        "train_end": train_end, "recent_n": recent_n,
        "sku_material": get_sku_material_map(clean_df),
        "new_set": new_sku_set(agg2, recent_n, train_end),
    }


def _predict_sku_value(ctx, sku, series, analogue_median, material, cat):
    """單一 SKU 的預測合計值（horizon 期相加）、是否為新品、是否需要彈性給位，共三項。

    非新品：多步驗證 fc_validate_multistep()——站在基準期一次往後推 horizon 期，過程中看不到
            測試期任何實際值，與需求預測頁 /api/forecast/series、/sku_top 用的是同一個函式。
    新品  ：冷啟動類比預測 cold_start_predict()。新品的序列前面幾乎全是 0（series_for 會把
            沒出貨的期補 0），直接餵時序選模會被這段零值前綴污染——MASE 分母失真、naive 只
            抓最後一期、holt 把剛上市的短期成長線性外插，預測值不具參考價值。需求預測頁對
            新品本來就改走冷啟動，配置用的預測值同樣照辦，兩邊才一致。

    新品又分兩種，儲位配置上的處置完全不同（見 place_after 的彈性給位說明）：
      1. 冷啟動水準取自「自身近期實際值」——訓練期內它自己就有出貨紀錄，只是期數不足以跑
         時序選模。這個水準是真實觀測值、可信，它應該照這個預測值跟其他商品正常競爭儲位。
      2. 冷啟動水準取自「同類別同區成熟品中位數」——訓練期內它完全沒有出貨，水準純粹是拿
         同類商品猜的，沒有任何自身證據。這種才需要彈性給位、不佔黃金區。
    第三個回傳值 needs_flex_slot 即代表第 2 種。"""
    if sku in ctx["new_set"]:
        lvl = analogue_median.get((cat, material), 0.0)
        cs = cold_start_predict(series, ctx["horizon"], lvl, ctx["train_end"])
        return sum(cs["pred"]), True, cs["source"] != COLD_START_SOURCE_OWN
    v = fc_validate_multistep(series, ctx["horizon"], ctx["train_end"])
    return sum(v["pred"]), False, False


def predicted_sku_values(clean_df, recent_n=COLD_START_RECENT_N):
    """算「每個 SKU 未來 horizon 期的預測合計」這個較貴的部分（每個 SKU 都要跑一次選模＋
    多步預測），刻意跟依門檻分級（cls/share/cum）拆開，好讓呼叫端（router）可以把這段結果
    快取起來——拉 ABC 門檻滑桿時不必重算預測，只有真的換了清洗結果才需要重跑
    （對應原 PRED_VALUES_CACHE）。

    貨架類依「預測揀貨次數」、棧板類依「預測出貨量」（貨架區重效率／棧板區重量體）；兩份
    清單各自獨立排序與分級、分開顯示，不會互相混用單位。

    母體只含「在實際用於預測的期別內有出貨」的 SKU（已排除尚未過完的末月），與需求預測頁
    ／商品生命週期分類同一個母體。
    回傳 {shelf:[{id,name,cat,freq,is_new}], pallet:[...], ...} 或 {insufficient:True}。"""
    ctx = _forecast_ctx(clean_df, recent_n)
    if ctx is None:
        return {"insufficient": True}
    agg = ctx["agg"]

    def build(material, total_series, series_map, target):
        analogue = analogue_levels(ctx["agg2"], ctx["sku_material"], target,
                                   ctx["train_end"], ctx["new_set"])["median"]
        cand = [(sku, val) for sku, val in total_series.items()
                if ctx["sku_material"].get(sku, "shelf") == material]
        cand.sort(key=lambda kv: -kv[1])
        rows = []
        for sku, _ in cand:
            s = series_for(series_map, ctx["periods"], key=sku)
            meta = agg["sku_meta"].get(sku, {})
            cat = meta.get("商品類別")
            pr, is_new, flex = _predict_sku_value(ctx, sku, s, analogue, material, cat)
            rows.append({"id": sku, "name": meta.get("商品名稱"), "cat": cat,
                         "freq": pr, "is_new": is_new, "needs_flex_slot": flex})
        rows.sort(key=lambda x: -x["freq"])
        return rows

    # 母體用「實際用於預測的期別」內的出貨合計，而不是 agg 的整份資料累計，這樣 ABC 分級頁
    # 的 SKU 檔數會和需求預測頁、商品生命週期分類一致（見 sku_totals_in_periods）。
    shelf = build("shelf", sku_totals_in_periods(agg, ctx["periods"], "cnt"), agg["by_sku_cnt"], "cnt")
    pallet = build("pallet", sku_totals_in_periods(agg, ctx["periods"], "qty"), agg["by_sku"], "qty")
    return {"shelf": shelf, "pallet": pallet,
            "horizon": ctx["horizon"], "n_periods": len(ctx["periods"]),
            "recent_n": ctx["recent_n"],
            "new_sku_count": sum(1 for r in shelf + pallet if r["is_new"]),
            "flex_slot_count": sum(1 for r in shelf + pallet if r["needs_flex_slot"])}


def predicted_sku_pick_counts(clean_df, recent_n=COLD_START_RECENT_N):
    """全部 SKU 一律用「預測揀貨次數」的 {商品ID: 預測合計}，供儲位配置的 ABC 分級與距離
    加權使用（見 sku_items_predicted_with_fallback）。

    為什麼這裡不沿用 predicted_sku_values() 的「貨架看次數／棧板看量體」：那兩份清單是各自
    獨立分級、獨立顯示的，單位不同沒有問題；但儲位配置會把全部 SKU 併成同一份清單，去算
    類別 ABC 與「Σ(次數×距離)/Σ次數」的加權平均距離——把「揀貨次數」和「出貨件數」兩種單位
    相加沒有意義。而儲位配置的目標函數就是「揀貨移動距離」，其權重本來就該是揀貨次數；
    棧板區「重量體」的考量已經體現在材積分類（classify_storage 決定貨架/棧板）與分區
    material 篩選上，不需要、也不該再混進距離權重裡。
    回傳 {freq:{sku:值}, horizon, n_periods, new_set} 或 None（期數不足以做多步驗證）。"""
    ctx = _forecast_ctx(clean_df, recent_n)
    if ctx is None:
        return None
    agg = ctx["agg"]
    analogue = analogue_levels(ctx["agg2"], ctx["sku_material"], "cnt",
                               ctx["train_end"], ctx["new_set"])["median"]
    out = {}
    for sku in agg["sku_total_cnt"].index:
        s = series_for(agg["by_sku_cnt"], ctx["periods"], key=sku)
        meta = agg["sku_meta"].get(sku, {})
        pr, _, _ = _predict_sku_value(ctx, sku, s, analogue,
                                      ctx["sku_material"].get(sku, "shelf"), meta.get("商品類別"))
        out[sku] = pr
    return {"freq": out, "horizon": ctx["horizon"], "n_periods": len(ctx["periods"]),
            "new_set": ctx["new_set"]}


def classify_predicted_sku(pred_values, a_thresh=70.0, b_thresh=90.0):
    """把 predicted_sku_values() 的結果依門檻分級（對應原 computePredictedSkuByZone 的門檻套用段）。"""
    if pred_values.get("insufficient"):
        return {"insufficient": True}

    def cls(arr):
        total = sum(x["freq"] for x in arr) or 1
        cum = 0.0
        out = []
        for x in arr:
            x = dict(x)
            share = x["freq"] / total * 100
            cum += share
            x["share"] = share
            x["cum"] = cum
            x["cls"] = "A" if cum <= a_thresh else ("B" if cum <= b_thresh else "C")
            out.append(x)
        return out

    return {"shelf": cls(pred_values["shelf"]), "pallet": cls(pred_values["pallet"]),
            "a_thresh": a_thresh, "b_thresh": b_thresh}


def compute_predicted_sku_by_zone(clean_df, a_thresh=70.0, b_thresh=90.0):
    """一次算完（不快取中間值）的版本，供不需要自行管理快取的呼叫端直接用
    （對應原 computePredictedSkuByZone）。"""
    return classify_predicted_sku(predicted_sku_values(clean_df), a_thresh, b_thresh)


def sku_items_predicted_with_fallback(clean_df):
    """「儲位配置」頁 ABC 分級與距離加權的基礎資料：改用預測值（每個 SKU 未來 horizon 期的
    **預測揀貨次數**）而非實際歷史出貨次數，對應本專案「歷史資料 → 需求預測 → 用預測結果做
    ABC／儲位配置」的設計目標。

    為什麼全部用「揀貨次數」而不是貨架看次數／棧板看量體：見 predicted_sku_pick_counts()
    的說明——儲位配置會把全部 SKU 併成同一份清單算類別 ABC 與加權平均距離，兩種單位相加
    沒有意義，而距離的權重本來就該是揀貨次數。

    新品（首次出貨落在基準期前 recent_n 期內）不會被排除，而是與需求預測頁一樣走冷啟動
    類比預測（見 _predict_sku_value），所以每一個 SKU 都拿得到有意義的預測值。

    仍會退回歷史值的只剩一種情況：該 SKU 出現在 sku_frequency()，但它的每一列都被
    compute_forecast_agg() 濾掉（出貨日期無法解析、或訂購數量<=0），連時間序列都建不出來。
    這時把它的歷史總次數**換算成同樣 horizon 期的水準**再頂替——歷史次數是整個資料期間
    （可能十幾個月）的累計，預測值只涵蓋 horizon 期（3 個月），不換算就直接混在同一份清單裡
    排序，這些列會被系統性高估好幾倍，平白擠進 A 級佔走黃金區。

    若整份資料的期數本身就不足以做多步驗證，則全部退回實際歷史頻率（等同尚未導入預測、
    維持原本行為，不讓頁面因此顯示不出結果）。

    回傳形狀與 sku_frequency()["items"] 相同（含 id/freq/code/name/cat），另附 is_new 與
    freq_source 兩個欄位供前端標示，可直接餵給 category_abc_from_sku()／
    apply_abc_thresholds() 等既有下游函式沿用。"""
    freq = sku_frequency(clean_df)
    if not freq or not freq["items"]:
        return freq

    pred = predicted_sku_pick_counts(clean_df)
    if not pred:
        return freq
    pred_freq = pred["freq"]
    # 歷史累計次數 →「同 horizon 期水準」的換算比例
    scale = (pred["horizon"] / pred["n_periods"]) if pred["n_periods"] else 1.0

    items = []
    for it in freq["items"]:
        it2 = dict(it)
        if it["id"] in pred_freq:
            it2["freq"] = pred_freq[it["id"]]
            it2["freq_source"] = "predicted"
        else:
            it2["freq"] = it["freq"] * scale
            it2["freq_source"] = "history_scaled"
        it2["is_new"] = it["id"] in pred["new_set"]
        items.append(it2)
    items.sort(key=lambda x: -x["freq"])
    return {"items": items, "order_items": freq["order_items"], "total_orders": freq["total_orders"]}


# ============================================================
# 10. 儲位配置（精細版：依「共同揀取關聯」把商品分群、群內相鄰擺放）
#     對應原 computeWhAssignment／whAllocateCategories／cwClusters／packBefore／placeAfter。
#     這一版是「10. 儲位配置」（第 6 節 compute_storage_assignment）的加強版：第 6 節只把
#     儲位「分給類別」（同類別商品視為同一堆，不分先後）；這一版進一步把「同一類別/同一
#     ABC 級距內，常被一起買的商品」排在彼此相鄰的儲位，供批次揀貨（第 11 節）估算更準的
#     移動距離、也是原型「儲位配置」頁「改善後地圖」的核心演算法。
# ============================================================

def _wh_area(zone):
    return zone.get("area_m2", 0.0)


def cw_clusters(ids, pairs):
    """簡易 union-find：把有「強關聯」（pairs）的 id 併成同一群（對應原 cwClusters）。"""
    parent = {i: i for i in ids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in pairs:
        if a in parent and b in parent:
            parent[find(a)] = find(b)

    groups = {}
    for i in ids:
        r = find(i)
        groups.setdefault(r, []).append(i)
    return list(groups.values())


def compute_wh_assignment(clean_df, cat_name_map, zones, a_thresh=70.0, b_thresh=90.0):
    """對應原 computeWhAssignment()。回傳：
      before_cat_zone：改善前，類別→分區代號（依面積配對，大類別配大區塊）。
      after_sku_zone ：改善後，SKU→分區代號（A/B 級依「共同揀取關聯」聚簇相鄰擺放；
                        C 級依「類別共同揀取關聯」聚簇，同類別商品盡量相鄰）。
      zone_before_cats／zone_after_class／after_prop：畫地圖／統計用的彙總。
      sku_info／sku_pred_cls：SKU 基本資料與預測 ABC 級別，供批次模擬（第 11 節）查詢。
    """
    freq = sku_frequency(clean_df)
    if not freq or not freq["items"]:
        return None
    # ABC 熱度分級／改善前類別配置改用「預測值」（本專案設計目標：歷史資料→需求預測→用
    # 預測結果做 ABC／儲位配置分析），歷史月數不足以預測的 SKU（新品）自動退回用實際歷史
    # 出貨次數頂替，確保每個 SKU 仍能被分級、分到儲位（見 sku_items_predicted_with_fallback
    # 內部說明）。這樣一來「改善前 vs 改善後」的差異單純是配置精細度（按類別粗放配置 vs
    # 按 SKU 熱度＋共同揀取關聯精細配置），不再混雜「歷史 vs 預測」這層差異。
    # 注意：下面 freq 本身（含 order_items）原封不動保留給 sku_copick() 做「共同揀取」市場籃
    # 分析——lift／support 是「實際歷史共同出現次數」的統計量，必須用真實歷史頻率才有意義，
    # 不能也不該用預測值取代。
    pred_freq = sku_items_predicted_with_fallback(clean_df)
    items = (pred_freq or freq)["items"]
    apply_abc_thresholds(items, a_thresh, b_thresh)

    pallet_zones = [z for z in zones if z.get("material") == "pallet"]
    shelf_zones = [z for z in zones if z.get("material") != "pallet"]
    cat_material = get_cat_material_map(clean_df)
    cat_abc = category_abc_from_sku(items, cat_name_map, a_thresh, b_thresh)

    before_cat_zone = {}

    def pack_before(material, zone_list):
        cs = sorted([c for c in cat_abc if cat_material.get(c["cat"], "shelf") == material],
                    key=lambda c: -c["sku_count"])
        if not zone_list:
            return
        rem = [{"zone": z, "cap": _wh_area(z)} for z in zone_list]
        tot_a = sum(r["cap"] for r in rem) or 1
        tot_s = sum(c["sku_count"] for c in cs) or 1
        for c in cs:
            rem.sort(key=lambda r: -r["cap"])
            t = rem[0]
            before_cat_zone[c["cat"]] = t["zone"]["id"]
            t["cap"] -= (c["sku_count"] / tot_s) * tot_a

    pack_before("pallet", pallet_zones)
    pack_before("shelf", shelf_zones)

    pred = compute_predicted_sku_by_zone(clean_df, a_thresh, b_thresh)
    after_sku_zone = {}
    after_sku_order = {}     # SKU → 擺放順序位次（位次相鄰＝儲位相鄰）
    after_sku_cell = {}      # SKU → 該分區內的格子序號
    cell_class = {}          # 分區代號 → {格子序號: 等級}，供前端逐格上色
    zone_items_class = {}

    # 每個儲位格到出貨點的繞行距離（公尺），與前端配置圖用同一份幾何算出。
    try:
        from . import wh_geometry
        cell_dists = wh_geometry.cell_distances()
    except Exception:
        cell_dists = {}
    sku_co = sku_copick(freq)
    cat_co = category_copick(clean_df, cat_name_map)

    def place_after(zone_list, pred_arr):
        if not pred_arr or not zone_list:
            return
        zs = sorted(zone_list, key=lambda z: z.get("distance_m", 0))
        # 「暫定彈性給位」：不佔用黃金區（A/B 帶），改併入依類別群聚的 C 帶暫放，待累積
        # 足夠歷史後隨實際表現重排——與商品查詢頁 _cold_start_card() 對使用者說明的處置一致。
        # 先前那張卡片只是純顯示，實際配置仍把新品排進 A/B 帶，卡片寫著「不佔用黃金區」、
        # 地圖上卻擺在黃金區，兩邊互相矛盾；這裡讓實際配置跟著照做。
        #
        # 條件是 needs_flex_slot 而不是 is_new：只有「訓練期內自己完全沒有出貨、水準純粹拿
        # 同類商品猜」的新品才彈性給位。若新品在訓練期內就有自己的出貨紀錄（冷啟動水準取自
        # 「自身近期實際值」），那是真實觀測值，它應該照預測值正常競爭儲位——實測把這種也
        # 一併趕去 C 帶，KPI 距離改善率會從 64.1% 掉到 59.7%（多走 10.7 萬公尺），因為其中
        # 不乏一上市就很熱、預測揀貨次數排進全體前 10% 的商品。
        a_items = [it for it in pred_arr if it["cls"] == "A" and not it.get("needs_flex_slot")]
        b_items = [it for it in pred_arr if it["cls"] == "B" and not it.get("needs_flex_slot")]
        c_items = [it for it in pred_arr if it["cls"] == "C" or it.get("needs_flex_slot")]
        freq_of = {it["id"]: it["freq"] for it in pred_arr}
        ab_items_map = {it["id"]: it for it in (a_items + b_items)}

        ab_pairs = []
        if sku_co:
            lift_thresh = sku_co["lift_count_thresh"]
            for p in sku_co["pairs"]:
                if p["lift"] >= 1.3 and p["count"] >= lift_thresh \
                        and p["a"]["id"] in ab_items_map and p["b"]["id"] in ab_items_map:
                    ab_pairs.append((p["a"]["id"], p["b"]["id"]))
        ab_clusters = cw_clusters(list(ab_items_map.keys()), ab_pairs)
        for g in ab_clusters:
            g.sort(key=lambda i: -freq_of.get(i, 0))

        # 擺放順序：純 A →（混合群的 A 成員）｜（混合群的 B 成員）→ 純 B
        #
        # 若把「A/B 混合群聚」整群連在一起排，群內的 A 與 B 必然相鄰，一群接一群排下去
        # 就會讓整條 A/B 交界帶變成紅橙交錯，看不出熱度由近而遠的分層。
        # 改成把混合群「拆開跨在交界上」：A 成員併入 A 帶的末端、B 成員併入 B 帶的開頭，
        # 且兩邊採鏡像順序（A 帶倒著放、B 帶正著放），於是同一群的 A 與 B 仍然隔著交界
        # 緊鄰——共同揀取要的相鄰性沒有損失，但 A 帶只有 A、B 帶只有 B，色帶乾淨分層。
        def _tier(g):
            classes = {ab_items_map[i]["cls"] for i in g}
            if classes == {"A"}:
                return 0
            if "A" in classes:
                return 1
            return 2

        by_freq = lambda g: -max((freq_of.get(i, 0) for i in g), default=0)
        pure_a = sorted([g for g in ab_clusters if _tier(g) == 0], key=by_freq)
        mixed = sorted([g for g in ab_clusters if _tier(g) == 1], key=by_freq)
        pure_b = sorted([g for g in ab_clusters if _tier(g) == 2], key=by_freq)

        order_ab = [ab_items_map[i] for g in pure_a for i in g]
        # 兩段都依「群內最高頻」由高到低排，熱門的仍然靠近出貨點；
        # 因為 A 段緊接在交界之前、B 段緊接在交界之後，同一群的 A 與 B 只隔著交界，
        # 距離差固定為「混合群數量」，仍然算相鄰擺放。
        for g in mixed:
            order_ab += [ab_items_map[i] for i in g if ab_items_map[i]["cls"] == "A"]
        for g in mixed:
            order_ab += [ab_items_map[i] for i in g if ab_items_map[i]["cls"] == "B"]
        order_ab += [ab_items_map[i] for g in pure_b for i in g]

        by_cat = {}
        for it in c_items:
            by_cat.setdefault(it["cat"], []).append(it)
        cat_freq = {c: sum(x["freq"] for x in arr) for c, arr in by_cat.items()}
        cat_codes = cat_co["cats"] if cat_co else []
        lf = cat_co["lift_matrix"] if cat_co else None
        cat_pairs = []
        if lf:
            for i in range(len(cat_codes)):
                for j in range(i + 1, len(cat_codes)):
                    if lf[i][j] >= 1.3 and cat_codes[i] in by_cat and cat_codes[j] in by_cat \
                            and cat_material.get(cat_codes[i], "shelf") == cat_material.get(cat_codes[j], "shelf"):
                        cat_pairs.append((cat_codes[i], cat_codes[j]))
        cat_clusters = cw_clusters(list(by_cat.keys()), cat_pairs)
        for g in cat_clusters:
            g.sort(key=lambda c: -cat_freq.get(c, 0))
        cat_clusters.sort(key=lambda g: -max((cat_freq.get(c, 0) for c in g), default=0))
        order_c = []
        for g in cat_clusters:
            for c in g:
                for it in sorted(by_cat[c], key=lambda x: -x["freq"]):
                    order_c.append(it)

        order = order_ab + order_c
        # 記下每個 SKU 在「擺放順序」中的位次：同一群聚（共同揀取關聯）的商品會被排在連續
        # 位次上，位次相鄰＝實體儲位相鄰。供商品查詢頁回答「關聯品有沒有被排在旁邊」。
        for pos, it in enumerate(order):
            after_sku_order[it["id"]] = pos

        # ---- 逐「儲位格」發放（取代原本逐「分區」依面積配額發放）----
        # 舊做法一個分區只用單一代表距離參與排序，整區拿到連續一段商品；但分區橫跨十幾公尺，
        # 於是相鄰兩格明明距離幾乎相同，卻因分屬不同區而落到不同等級（R6 全 C、隔壁 R9 有 A/B）。
        # 改成把同材質所有格子依「各自的繞行距離」跨區混排，商品依熱度順序一路發下去，
        # 等級邊界就會沿著真實的距離等高線走，跨區也連續。
        zone_ids = {z["id"] for z in zs}
        slots = []          # (距離, 分區代號, 格子序號)
        for code, arr in cell_dists.items():
            if code in zone_ids:
                for ci, d in enumerate(arr):
                    slots.append((d, code, ci))
        slots.sort(key=lambda s: s[0])

        n = len(order)
        if not slots or not n:
            return
        # 每個商品佔用「一段連續的格子」（它的儲位面積），而不是單一格：
        # 商品數遠少於格子數，若一個商品只佔一格，整個倉庫會幾乎全空、也看不出配置意圖。
        # 依序把格子由近到遠切成 n 段，第 i 熱的商品拿第 i 段——最熱的就在最靠近出貨點的位置，
        # 且因為格子是「跨分區依實際距離混排」的，等級邊界會沿著距離等高線走。
        total = len(slots)
        for idx, it in enumerate(order):
            start = idx * total // n
            end = (idx + 1) * total // n if idx < n - 1 else total
            if end <= start:
                end = min(total, start + 1)
            home_code, home_cell = slots[start][1], slots[start][2]
            after_sku_zone[it["id"]] = home_code      # 以最靠近出貨點的那一格代表商品位置
            after_sku_cell[it["id"]] = home_cell
            # 地圖著色與分區統計要用「實際擺放等級」：彈性給位的商品被暫放在 C 帶，就照
            # C 上色，否則 C 帶中間會冒出幾格 A 色、與它實際所在的位置不符。它的預測等級仍
            # 完整保留在 sku_pred_cls（商品查詢頁顯示用），沒有被這裡蓋掉。
            pcls = "C" if it.get("needs_flex_slot") else it["cls"]
            for si in range(start, end):
                _, code, ci = slots[si]
                cell_class.setdefault(code, {})[ci] = pcls
            # 一個商品的儲位可能橫跨相鄰分區，計數時只記在它的代表分區，避免重複灌水
            zc = zone_items_class.setdefault(home_code, {"A": 0, "B": 0, "C": 0})
            zc[pcls] += 1

    if pred and not pred.get("insufficient"):
        place_after(shelf_zones, pred["shelf"])
        place_after(pallet_zones, pred["pallet"])

    for it in items:
        if it["id"] not in after_sku_zone:
            bc = before_cat_zone.get(it["cat"])
            if bc:
                after_sku_zone[it["id"]] = bc

    zone_after_class = {}
    for code, zc in zone_items_class.items():
        if zc["A"] >= zc["B"] and zc["A"] >= zc["C"]:
            zone_after_class[code] = "A"
        elif zc["B"] >= zc["C"]:
            zone_after_class[code] = "B"
        else:
            zone_after_class[code] = "C"

    zone_by_id = {z["id"]: z for z in zones}
    after_prop = {"shelf": {"A": 0, "B": 0, "C": 0}, "pallet": {"A": 0, "B": 0, "C": 0}}
    for code, zc in zone_items_class.items():
        z = zone_by_id.get(code)
        mat = "pallet" if (z and z.get("material") == "pallet") else "shelf"
        for k in ("A", "B", "C"):
            after_prop[mat][k] += zc[k]
    for m in ("shelf", "pallet"):
        o = after_prop[m]
        t = o["A"] + o["B"] + o["C"] or 1
        after_prop[m] = {k: o[k] / t for k in ("A", "B", "C")}

    zone_before_cats = {}
    for cat, code in before_cat_zone.items():
        zone_before_cats.setdefault(code, []).append(category_label(cat, cat_name_map))

    sku_info = {it["id"]: {"cat": it["cat"], "freq": it["freq"], "cls": it["cls"], "name": it["name"]}
                for it in items}
    sku_pred_cls = {}
    new_sku_ids = []
    flex_slot_sku_ids = []
    if pred and not pred.get("insufficient"):
        for it in pred["shelf"] + pred["pallet"]:
            sku_pred_cls[it["id"]] = it["cls"]
            if it.get("is_new"):
                new_sku_ids.append(it["id"])
            if it.get("needs_flex_slot"):
                flex_slot_sku_ids.append(it["id"])

    return {
        "before_cat_zone": before_cat_zone, "after_sku_zone": after_sku_zone,
        "after_sku_order": after_sku_order,
        # 走冷啟動預測的新品清單，供前端在地圖／表格上標示。
        "new_sku_ids": new_sku_ids,
        # 其中「自身完全沒有出貨紀錄」而採彈性給位（暫放 C 帶、不佔黃金區）的子集合。
        "flex_slot_sku_ids": flex_slot_sku_ids,
        "zone_before_cats": zone_before_cats, "zone_after_class": zone_after_class,
        "after_prop": after_prop, "sku_info": sku_info, "sku_pred_cls": sku_pred_cls,
        # 每個分區實際被指派到的 A/B/C 商品「數量」（不只多數決後的單一級別）。
        "zone_items_class": zone_items_class,
        # 逐格等級：{分區代號: {格子序號: 'A'|'B'|'C'}}。前端配置圖直接依這份上色，
        # 顏色即為實際指派結果，等級邊界會沿著真實距離等高線跨區連續。
        "cell_class": cell_class,
        "after_sku_cell": after_sku_cell,
        "sku_copick": sku_co,
    }


# ============================================================
# 11. 批次併單模擬（地理分群＋播種式批次；依實際儲位分區估算移動距離）
#     對應原 buildBatchOrders／batchWindowMonths／batchBuildWaves／computeDayPlan／
#     computeBatchStrategies。取代第 5 節 batch_simulate() 的「只算回合數改善率」陽春版——
#     這一版依「出貨日期 × 配送區域」把訂單分波（波內夠大就地併單、太零散併入同日同縣市
#     池），每一波再用「播種式揀貨」（依儲位距離排序、依容量切滿每回合）估算揀貨回合數與
#     移動距離，並可查詢特定出貨日期的詳細揀貨計畫（第一線班表可直接使用）。
# ============================================================

def _split_city_district(region):
    """把 cleaning_core 清洗階段產出的「配送區域」欄位（縣市＋行政區合併字串，例如
    「新北市內湖區」）拆回 (縣市, 行政區) 兩段，供批次分波用（對應原 batchParseGeo 的
    輸出，但地址正規化本身已由 cleaning_core 做完，這裡只是單純字串切分）。"""
    if not region:
        return "未知", "未知"
    s = str(region).strip()
    if not s:
        return "未知", "未知"
    m = CURRENT_CITY_RE.match(s)
    if m:
        city = m.group(0)
        rest = s[len(city):].strip()
        return city, (rest or "未知")
    return s, "未知"


def build_batch_orders(clean_df):
    """把清洗後資料依「系統出貨單號」彙總成批次模擬要用的訂單清單（對應原 buildBatchOrders）。
    回傳 {訂單號: {date(YYYYMMDD字串), region(配送區域), lines(明細行數),
                   skus({商品ID:明細行數}), qty({商品ID:訂購數量合計})}}。"""
    import pandas as pd
    df = clean_df.dropna(subset=["系統出貨單號"])
    if df.empty:
        return {}
    df = df.copy()
    if "出貨日期" in df.columns:
        df["_date"] = pd.to_datetime(df["出貨日期"], errors="coerce").dt.strftime("%Y%m%d").fillna("")
    else:
        df["_date"] = ""
    if "配送區域" in df.columns:
        df["_region"] = df["配送區域"].fillna("未知").replace("", "未知")
    else:
        df["_region"] = "未知"

    meta = df.groupby("系統出貨單號", sort=False).agg(
        date=("_date", "first"), region=("_region", "first"), lines=("_date", "size"))

    # 用 itertuples 取代 iterrows：巨量列數（實測百萬列訂單明細）時 iterrows 逐列建立 Series
    # 的開銷很重，itertuples 用 namedtuple、快非常多，是這支函式在大檔案時的主要效能瓶頸。
    orders = {ord_id: {"date": row.date, "region": row.region, "lines": int(row.lines),
                        "skus": {}, "qty": {}}
              for ord_id, row in zip(meta.index, meta.itertuples(index=False))}

    sub = df.dropna(subset=["商品ID"])
    if not sub.empty:
        has_qty = "訂購數量" in sub.columns
        agg_spec = {"cnt": ("商品ID", "size")}
        if has_qty:
            agg_spec["qty"] = ("訂購數量", "sum")
        sku_agg = sub.groupby(["系統出貨單號", "商品ID"], sort=False).agg(**agg_spec).reset_index()
        for row in sku_agg.itertuples(index=False):
            e = orders.get(row.系統出貨單號)
            if e is None:
                continue
            e["skus"][row.商品ID] = int(row.cnt)
            e["qty"][row.商品ID] = float(row.qty) if has_qty else 0.0
    return orders


def batch_window_months(orders):
    """依訂單資料的日期範圍，取「最近 3 個完整月」作為地理分群批次的預設觀察窗
    （對應原 batchWindowMonths：若最新一筆日期不是當月最後一天，代表當月資料還沒收全，
    排除掉，避免用不完整的月份誤判分群密度）。回傳月份字串（YYYYMM）集合，空集合代表
    「資料不足 3 個完整月，不篩選、用全部資料」。"""
    import calendar
    months = set()
    max_ymd = ""
    for e in orders.values():
        d = e["date"]
        if not d:
            continue
        months.add(d[:6])
        if d > max_ymd:
            max_ymd = d
    arr = sorted(months)
    if max_ymd and len(max_ymd) == 8:
        y, m, day = int(max_ymd[:4]), int(max_ymd[4:6]), int(max_ymd[6:8])
        last_day = calendar.monthrange(y, m)[1]
        if day < last_day:
            lm = max_ymd[:6]
            arr = [x for x in arr if x != lm]
    return set(arr[-3:])


def _dist_of(zone_codes, zone_dist):
    """一組分區代號的「來回移動距離」合計（對應原 distOf：每個分區距離 ×2 代表來回）。"""
    d = 0.0
    for zc in zone_codes:
        zd = zone_dist.get(zc)
        if zd is not None:
            d += zd * 2
    return d


def batch_build_waves(orders, capacity, threshold_n, zone_dist, sku_zone, before_zone=None, day_only=None):
    """依（出貨日期 × 縣市 × 行政區）把訂單分波：一波訂單數 ≥ threshold_n 就地併單；
    不足則併入「同日同縣市」的稀疏池。每一波內用播種式揀貨（依儲位距離排序、依 capacity
    切滿每回合）估算揀貨回合數與移動距離，並與逐單揀貨比較（對應原 batchBuildWaves）。
    day_only 傳入 YYYYMMDD 字串時只計算單日（對應原 computeDayBatch）。"""
    win = None if day_only else batch_window_months(orders)
    dg = {}
    for e in orders.values():
        date = e["date"]
        if day_only:
            if date != day_only:
                continue
        elif win and date[:6] not in win:
            continue
        city, dist = _split_city_district(e["region"])
        k = (date, city, dist)
        g = dg.setdefault(k, {"date": date, "city": city, "dist": dist, "list": []})
        g["list"].append(e)

    waves = []
    pool = {}
    for g in dg.values():
        if len(g["list"]) >= threshold_n:
            waves.append({"date": g["date"], "city": g["city"], "dist": g["dist"], "type": "dense", "list": g["list"]})
        else:
            pk = (g["date"], g["city"])
            pw = pool.setdefault(pk, {"date": g["date"], "city": g["city"], "dist": "（稀疏併縣市）",
                                       "type": "pool", "list": []})
            pw["list"].extend(g["list"])
    waves.extend(pool.values())

    single_rounds = 0
    single_dist = 0.0
    single_before_dist = 0.0
    seed_rounds = 0
    seed_dist = 0.0
    seed_picks = 0
    total_lines = 0
    rows = []
    for w in waves:
        w_lines = 0
        for e in w["list"]:
            single_rounds += 1
            w_lines += e["lines"]
            total_lines += e["lines"]
            zs, bzs = set(), set()
            for sku in e["skus"]:
                z = sku_zone.get(sku) if sku_zone else None
                if z:
                    zs.add(z)
                if before_zone:
                    bz = before_zone.get(sku)
                    if bz:
                        bzs.add(bz)
            single_dist += _dist_of(zs, zone_dist)
            single_before_dist += _dist_of(bzs, zone_dist)

        wave_qty = {}
        for e in w["list"]:
            for sku, q in e["qty"].items():
                wave_qty[sku] = wave_qty.get(sku, 0.0) + q
        ids = sorted(wave_qty.keys(),
                     key=lambda sku: zone_dist.get(sku_zone.get(sku), 9999) if sku_zone else 9999)
        wr, wd = 0, 0.0
        if ids:
            for i in range(0, len(ids), capacity):
                chunk = ids[i:i + capacity]
                wr += 1
                zs = set()
                for sku in chunk:
                    z = sku_zone.get(sku) if sku_zone else None
                    if z:
                        zs.add(z)
                wd += _dist_of(zs, zone_dist)
        elif w["list"]:
            wr = 1
        seed_rounds += wr
        seed_dist += wd
        seed_picks += len(ids)
        rows.append({"date": w["date"], "city": w["city"], "dist": w["dist"], "type": w["type"],
                     "orders": len(w["list"]), "lines": w_lines, "sku_count": len(ids),
                     "rounds": wr, "wave_dist": wd})

    return {
        "single_rounds": single_rounds, "single_dist": single_dist, "single_before_dist": single_before_dist,
        "seed_rounds": seed_rounds, "seed_dist": seed_dist, "seed_picks": seed_picks,
        "total_lines": total_lines, "waves": rows,
        "order_count": sum(r["orders"] for r in rows),
    }


def compute_day_batch(orders, day, capacity, threshold_n, zone_dist, sku_zone, before_zone=None):
    return batch_build_waves(orders, capacity, threshold_n, zone_dist, sku_zone, before_zone, day_only=day)


def compute_day_plan(orders, day, capacity, threshold_n, zone_dist, sku_zone, sku_info=None):
    """單日揀貨計畫：每波依「小單優先」排出 SKU 揀取序列，再依容量切滿每回合，回合內依
    儲位距離排序（近似最短路徑），可直接當第一線揀貨班表使用（對應原 computeDayPlan）。"""
    dg = {}
    for e in orders.values():
        if e["date"] != day:
            continue
        city, dist = _split_city_district(e["region"])
        k = (city, dist)
        g = dg.setdefault(k, {"city": city, "dist": dist, "list": []})
        g["list"].append(e)

    waves = []
    pool = {}
    for g in dg.values():
        if len(g["list"]) >= threshold_n:
            waves.append({"city": g["city"], "dist": g["dist"], "type": "dense", "list": g["list"]})
        else:
            pw = pool.setdefault(g["city"], {"city": g["city"], "dist": "（稀疏併縣市）",
                                              "type": "pool", "list": []})
            pw["list"].extend(g["list"])
    waves.extend(pool.values())

    out = []
    for w in waves:
        order_sets = [list(e["skus"].keys()) for e in w["list"]]
        wave_qty = {}
        for e in w["list"]:
            for sku, q in e.get("qty", {}).items():
                wave_qty[sku] = wave_qty.get(sku, 0.0) + q

        order_idx = sorted(range(len(order_sets)), key=lambda i: len(order_sets[i]))
        seen, seq0 = set(), []
        for oi in order_idx:
            for sku in order_sets[oi]:
                if sku not in seen:
                    seen.add(sku)
                    seq0.append(sku)
        rounds = [seq0[i:i + capacity] for i in range(0, len(seq0), capacity)]
        for r in rounds:
            r.sort(key=lambda sku: zone_dist.get(sku_zone.get(sku), 9999) if sku_zone else 9999)

        sku_round = {}
        for ri, r in enumerate(rounds):
            for sku in r:
                sku_round[sku] = ri
        complete_r = []
        for arr in order_sets:
            mx = 0
            for sku in arr:
                rr = sku_round.get(sku, 0)
                if rr > mx:
                    mx = rr
            complete_r.append(mx)
        n_r = len(rounds)
        done_at = [0] * n_r
        for rr in complete_r:
            if n_r:
                done_at[rr] += 1
        run = 0
        cum_by_round = []
        for v in done_at:
            run += v
            cum_by_round.append(run)
        tot = len(order_sets)

        serves = {}
        for arr in order_sets:
            for sku in set(arr):
                serves[sku] = serves.get(sku, 0) + 1

        seq, round_summary = [], []
        move_dist = 0.0
        for ri, r in enumerate(rounds):
            zs = set()
            for sku in r:
                z = sku_zone.get(sku) if sku_zone else None
                if z:
                    zs.add(z)
            rd = _dist_of(zs, zone_dist)
            move_dist += rd
            cum_done = cum_by_round[ri] if ri < len(cum_by_round) else 0
            round_summary.append({"round": ri + 1, "sku_count": len(r), "zones": len(zs), "dist": rd,
                                  "cum_done": cum_done, "cum_pct": (cum_done / tot * 100) if tot else 0.0})
            for sku in r:
                info = (sku_info or {}).get(sku) or {}
                seq.append({"id": sku, "name": info.get("name", ""),
                            "zone": (sku_zone.get(sku) if sku_zone else None) or "—",
                            "serves": serves.get(sku, 0), "qty": wave_qty.get(sku, 0.0), "round": ri + 1})

        out.append({"city": w["city"], "dist": w["dist"], "type": w["type"], "orders": tot,
                    "lines": sum(e["lines"] for e in w["list"]), "sku_count": len(seq),
                    "rounds": len(rounds), "move_dist": move_dist,
                    "round_summary": round_summary, "seq": seq})

    out.sort(key=lambda w: -w["orders"])
    return out


def compute_batch_strategies(orders, capacity, threshold_n, zone_dist, sku_zone, before_zone=None):
    """依目前容量／分波門檻算出「逐單 vs 播種式批次」的整體比較，以及地理分佈 Top15、
    各波 SKU 數中位數、容量卡到的波比例等輔助指標（對應原 computeBatchStrategies）。"""
    w = batch_build_waves(orders, capacity, threshold_n, zone_dist, sku_zone, before_zone, day_only=None)

    def imp(b, v):
        return (b - v) / b * 100 if b > 0 else 0.0

    geo_agg = {}
    dates = set()
    win = batch_window_months(orders)
    for e in orders.values():
        if e["date"]:
            dates.add(e["date"])
        if win and e["date"][:6] not in win:
            continue
        city, dist = _split_city_district(e["region"])
        gk = (city, dist)
        ga = geo_agg.setdefault(gk, {"city": city, "dist": dist, "orders": 0, "lines": 0})
        ga["orders"] += 1
        ga["lines"] += e["lines"]

    wm = sorted(win) if win else []
    window_label = (f"{wm[0][:4]}-{wm[0][4:6]} ～ {wm[-1][:4]}-{wm[-1][4:6]}") if wm else "全部"

    sc = sorted(r["sku_count"] for r in w["waves"])
    n_g = len(sc)
    if n_g:
        sku_median = sc[(n_g - 1) // 2] if n_g % 2 else (sc[n_g // 2 - 1] + sc[n_g // 2]) / 2
    else:
        sku_median = 0
    groups = len(w["waves"])
    bind_groups = sum(1 for r in w["waves"] if r["sku_count"] > capacity)
    geo_top = sorted(geo_agg.values(), key=lambda g: -g["orders"])[:15]

    return {
        "total_orders": w["order_count"], "total_lines": w["total_lines"], "groups": groups,
        "capacity": capacity, "threshold": threshold_n, "geo_top": geo_top, "sku_median": sku_median,
        "bind_groups": bind_groups, "bind_ratio": (bind_groups / groups * 100) if groups else 0.0,
        "dates": sorted(dates), "window_label": window_label,
        "single": {"rounds": w["single_rounds"], "dist": w["single_dist"]},
        "single_before": {"dist": w["single_before_dist"]},
        "seed_picks": w["seed_picks"], "total_lines_raw": w["total_lines"],
        "seed": {"rounds": w["seed_rounds"], "dist": w["seed_dist"],
                 "imp_rounds": imp(w["single_rounds"], w["seed_rounds"]),
                 "imp_dist": imp(w["single_dist"], w["seed_dist"])},
    }


def batch_geo_context(clean_df, cat_name_map, zones, a_thresh=70.0, b_thresh=90.0):
    """組裝地理分群批次模擬要用的 zone_dist／sku_zone／before_zone／sku_info——這些原本是
    原型的全域變數（WHZ／WH_ASSIGN_CACHE），這裡改成呼叫 compute_wh_assignment() 現算現組
    （對應原 whZoneMaps）。呼叫端應把這個結果快取起來（依 a_thresh/b_thresh 當 key），
    避免每次拉批次容量滑桿都重跑一次完整的儲位配置＋預測式ABC演算法。"""
    zone_dist = {z["id"]: z.get("distance_m", 0) for z in zones}
    wh = compute_wh_assignment(clean_df, cat_name_map, zones, a_thresh, b_thresh)
    if not wh:
        return {"zone_dist": zone_dist, "sku_zone": {}, "before_zone": {}, "sku_info": {}, "wh": None}
    before_zone = {}
    for sku_id, info in wh["sku_info"].items():
        bc = wh["before_cat_zone"].get(info["cat"])
        if bc:
            before_zone[sku_id] = bc
    return {"zone_dist": zone_dist, "sku_zone": wh["after_sku_zone"], "before_zone": before_zone,
            "sku_info": wh["sku_info"], "wh": wh}
