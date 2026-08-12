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

def sku_frequency(clean_df):
    """回傳 {items, order_items, total_orders}。
    items：依「出現在幾張不同訂單」由高到低排序的 SKU 清單（對應原 computeSkuFreqData）。
    order_items：{系統出貨單號: {商品ID,...}}，供 sku_copick() 共用揀取分析使用。"""
    df = clean_df.dropna(subset=["系統出貨單號", "商品ID"])
    if df.empty:
        return None
    order_items = df.groupby("系統出貨單號")["商品ID"].apply(lambda s: set(s.dropna())).to_dict()

    item_freq = defaultdict(int)
    for ids in order_items.values():
        for iid in ids:
            item_freq[iid] += 1

    meta_df = df.drop_duplicates("商品ID").set_index("商品ID")
    items = []
    for iid, freq in item_freq.items():
        row = meta_df.loc[iid] if iid in meta_df.index else None
        items.append({
            "id": iid, "freq": freq,
            "code": (row["商品編號"] if row is not None else ""),
            "name": (row["商品名稱"] if row is not None else ""),
            "cat": (row["商品類別"] if row is not None else None),
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
    order_cats = df.groupby("系統出貨單號")["商品類別"].apply(lambda s: set(s.dropna())).to_dict()

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


FC_TEST_MONTH = 3
FC_TEST_WEEK = 13


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


# ============================================================
# 4. 商品生命週期分類（新品／滯銷／正常）
# ============================================================

def lifecycle_classify(agg, recent_n):
    periods = agg["periods"]
    n = len(periods)
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
        cls = "新品" if first >= n - recent_n else ("滯銷" if last <= n - 1 - recent_n else "正常")
        rows.append({
            "sku": sku, "code": meta.get("商品編號"), "name": meta.get("商品名稱"),
            "cat": meta.get("商品類別"), "first": periods[first], "last": periods[last],
            "nz": nz, "dorm_months": dorm_months, "cls": cls,
        })
    counts = {"新品": 0, "滯銷": 0, "正常": 0}
    for r in rows:
        counts[r["cls"]] += 1
    return {"rows": rows, "counts": counts, "periods": periods, "recent_n": recent_n}


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

def compute_kpi(storage_result, batch_result, unit_count=3, speed=1.0, handle_sec=12.0, mode="manual"):
    """對應原 runStorageSimulation()：整合儲位配置改善結果與批次併單結果，
    算出移動距離／揀貨工時改善率，並與專案章程訂下的 KPI 目標（距離≥10%、
    回合數≥10%、工時≥5%）比對。"""
    orig_dist = storage_result["baseline"]
    opt_dist = storage_result["weighted"]
    dist_improvement = (orig_dist - opt_dist) / orig_dist * 100 if orig_dist else 0.0

    agv_factor = 1.35 if mode == "agv" else 1.0
    eff_speed = max(1.0, speed * agv_factor)

    def worktime_minutes(dist_m, rounds):
        if not rounds:
            return 0.0
        avg_lines = batch_result["total_lines"] / rounds
        move_min = (dist_m * 2 * rounds) / eff_speed
        handle_min = (avg_lines * handle_sec / 60) * rounds
        return (move_min + handle_min) / max(1, unit_count)

    orig_worktime = worktime_minutes(orig_dist, batch_result["single_rounds"])
    opt_worktime = worktime_minutes(opt_dist, batch_result["batch_rounds"])
    worktime_improvement = (orig_worktime - opt_worktime) / orig_worktime * 100 if orig_worktime else 0.0

    return {
        "mode": mode, "unit_count": unit_count, "speed": speed, "handle_sec": handle_sec,
        "orig_dist": orig_dist, "opt_dist": opt_dist, "dist_improvement": dist_improvement,
        "orig_worktime": orig_worktime, "opt_worktime": opt_worktime,
        "worktime_improvement": worktime_improvement,
        "single_rounds": batch_result["single_rounds"], "batch_rounds": batch_result["batch_rounds"],
        "rounds_improvement": batch_result["improvement"], "capacity": batch_result["capacity"],
        "targets": {"dist": 10.0, "rounds": 10.0, "worktime": 5.0},
        "targets_met": {
            "dist": dist_improvement >= 10.0,
            "rounds": batch_result["improvement"] >= 10.0,
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
    periods = _predicted_periods(agg)
    test_len = FC_TEST_MONTH
    if len(periods) < test_len + 4:
        return {"insufficient": True}

    cat_mat = get_cat_material_map(clean_df)
    cats = set(agg["by_cat"].index.get_level_values(0).unique()) | set(agg["by_cat_cnt"].index.get_level_values(0).unique())
    rows = []
    for c in cats:
        sc = series_for(agg["by_cat_cnt"], periods, key=c)
        sq = series_for(agg["by_cat"], periods, key=c)
        if len(sc) < test_len + 4:
            continue
        vc = fc_validate(sc, test_len)
        vq = fc_validate(sq, test_len)
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


def predicted_sku_values(clean_df):
    """算「每個 SKU 最後一期預測值」這個較貴的部分（對每個候選 SKU 都要跑一次 fc_validate），
    刻意跟依門檻分級（cls/share/cum）拆開，好讓呼叫端（router）可以把這段結果快取起來——
    拉 ABC 門檻滑桿時不必重算預測，只有真的換了清洗結果才需要重跑（對應原 PRED_VALUES_CACHE）。
    回傳 {shelf:[{id,name,cat,freq}], pallet:[...]} 或 {insufficient:True}。"""
    agg = compute_forecast_agg(clean_df, "month")
    if not agg or not agg["periods"]:
        return {"insufficient": True}
    periods = _predicted_periods(agg)
    test_len = FC_TEST_MONTH
    if len(periods) < test_len + 4:
        return {"insufficient": True}

    sku_mat = get_sku_material_map(clean_df)

    def build(material, total_series, series_map):
        cand = [(sku, val) for sku, val in total_series.items() if sku_mat.get(sku, "shelf") == material]
        cand.sort(key=lambda kv: -kv[1])
        rows = []
        for sku, _ in cand:
            s = series_for(series_map, periods, key=sku)
            if len(s) < test_len + 4:
                continue
            v = fc_validate(s, test_len)
            pr = sum(v["pred"])
            meta = agg["sku_meta"].get(sku, {})
            rows.append({"id": sku, "name": meta.get("商品名稱"), "cat": meta.get("商品類別"), "freq": pr})
        rows.sort(key=lambda x: -x["freq"])
        return rows

    shelf = build("shelf", agg["sku_total_cnt"], agg["by_sku_cnt"])
    pallet = build("pallet", agg["sku_total"], agg["by_sku"])
    return {"shelf": shelf, "pallet": pallet}


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
    items = freq["items"]
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
    zone_items_class = {}
    sku_co = sku_copick(freq)
    cat_co = category_copick(clean_df, cat_name_map)

    def place_after(zone_list, pred_arr):
        if not pred_arr or not zone_list:
            return
        zs = sorted(zone_list, key=lambda z: z.get("distance_m", 0))
        a_items = [it for it in pred_arr if it["cls"] == "A"]
        b_items = [it for it in pred_arr if it["cls"] == "B"]
        c_items = [it for it in pred_arr if it["cls"] == "C"]
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
        ab_clusters.sort(key=lambda g: -max((freq_of.get(i, 0) for i in g), default=0))
        order_ab = [ab_items_map[i] for g in ab_clusters for i in g]

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
        total_area = sum(_wh_area(z) for z in zs) or 1
        n = len(order)
        idx = 0
        for zi, z in enumerate(zs):
            quota = max(1, round(n * _wh_area(z) / total_area))
            end = n if zi == len(zs) - 1 else min(n, idx + quota)
            while idx < end:
                it = order[idx]
                after_sku_zone[it["id"]] = z["id"]
                zc = zone_items_class.setdefault(z["id"], {"A": 0, "B": 0, "C": 0})
                zc[it["cls"]] += 1
                idx += 1
        while idx < n:
            it = order[idx]
            z = zs[-1]
            after_sku_zone[it["id"]] = z["id"]
            zc = zone_items_class.setdefault(z["id"], {"A": 0, "B": 0, "C": 0})
            zc[it["cls"]] += 1
            idx += 1

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
    if pred and not pred.get("insufficient"):
        for it in pred["shelf"] + pred["pallet"]:
            sku_pred_cls[it["id"]] = it["cls"]

    return {
        "before_cat_zone": before_cat_zone, "after_sku_zone": after_sku_zone,
        "zone_before_cats": zone_before_cats, "zone_after_class": zone_after_class,
        "after_prop": after_prop, "sku_info": sku_info, "sku_pred_cls": sku_pred_cls,
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
