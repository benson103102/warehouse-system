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

輸入皆為 cleaning_core.run_cleaning_pipeline() 產生的乾淨資料 DataFrame（含出貨日期／
商品ID／商品編號／商品名稱／商品類別／系統出貨單號／訂購數量 等欄位），輸出為可直接
序列化成 JSON 的 dict／list，供 API 路由回傳。

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
