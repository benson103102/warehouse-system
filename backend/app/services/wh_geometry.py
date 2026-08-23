"""倉儲平面幾何 — 讀取 frontend/warehouse_geometry.js，算出每個「儲位格」到出貨點的繞行距離。

為什麼後端要自己算：
    儲位配置原本是「SKU → 分區」，一個分區只用單一代表距離參與排序。但一個分區實際橫跨
    十幾公尺（例如 R6 從 21.6m 到 32.7m），用一個數字代表它，必然在分區邊界產生跳階——
    R6 與 R9 相鄰的角落距離只差 0.5m，卻因為分屬不同區而落到不同等級。
    改成「SKU → 儲位格」後，等級邊界會沿著真實距離等高線走，跨區也連續。

為什麼不把距離烘成靜態資料：
    幾何檔若重繪，烘好的距離就會與畫面不同步，而這種不同步很難被發現（正是上面那個 bug
    的成因）。這裡直接讀同一份幾何檔、用與前端 computeCellRoutedDist() 相同的演算法計算，
    前後端必然一致。結果會快取在記憶體，只有第一次呼叫時計算。

演算法與前端 computeCellRoutedDist() 逐項對齊：
    12px 網格 → 標記可走格（在外牆多邊形內、且不在任何分區／設施矩形內）
    → 從出貨點 BFS（四方向）→ 每個儲位格取「最近的可走格 BFS 步數 ＋ 到該格的直線距離」。
    跨越 y=985／y=1278 兩條界線時只能走特定開口（gY985／gY1278），對應現場的出入口。
"""

import heapq
import json
import math
import os
import re
import threading

_GS = 12          # 網格邊長（px），與前端相同
_LOCK = threading.Lock()
_CACHE = None

# 跨越倉別界線的開口（x 範圍），與前端 crossOK() 相同
_GATES = {985: [(865, 1000), (1430, 1558)], 1278: [(808, 1012)]}


def _geometry_path():
    env = os.environ.get("WAREHOUSE_FRONTEND_DIR")
    if env:
        p = os.path.join(env, "warehouse_geometry.js")
        if os.path.isfile(p):
            return p
    return os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "frontend", "warehouse_geometry.js"))


def load_geometry():
    """把 `const WH_DATA = {...};` 這個 JS 檔剖成 dict（檔案本體就是合法 JSON）。"""
    path = _geometry_path()
    with open(path, encoding="utf-8") as f:
        text = f.read()
    m = re.search(r"const\s+WH_DATA\s*=\s*", text)
    if not m:
        raise ValueError(f"無法解析幾何檔（找不到 WH_DATA 宣告）：{path}")
    body = text[m.end():].strip()
    if body.endswith(";"):
        body = body[:-1]
    return json.loads(body)


def _in_poly(x, y, poly):
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i][0], poly[i][1]
        xj, yj = poly[j][0], poly[j][1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _cross_ok(x, ya, yb):
    lo, hi = (ya, yb) if ya <= yb else (yb, ya)
    for line, gates in _GATES.items():
        if lo < line <= hi and not any(g[0] <= x <= g[1] for g in gates):
            return False
    return True


def _compute():
    geo = load_geometry()
    vb = geo["viewBox"]
    poly = geo["outline"]
    px_per_m = float(geo.get("meta", {}).get("pxPerMeter") or 40.0)

    cols = math.ceil(vb["w"] / _GS)
    rows = math.ceil(vb["h"] / _GS)

    def nx(gx):
        return vb["x"] + gx * _GS + _GS / 2

    def ny(gy):
        return vb["y"] + gy * _GS + _GS / 2

    blockers = list(geo["zones"]) + [f for f in geo["facilities"] if f.get("kind") != "origin"]

    def walkable(x, y):
        if not _in_poly(x, y, poly):
            return False
        for b in blockers:
            if b["x"] <= x <= b["x"] + b["w"] and b["y"] <= y <= b["y"] + b["h"]:
                return False
        return True

    walk = [False] * (cols * rows)
    for gy in range(rows):
        for gx in range(cols):
            walk[gy * cols + gx] = walkable(nx(gx), ny(gy))

    origin = next((f for f in geo["facilities"] if f.get("kind") == "origin"), None)
    if origin is None:
        return {"cells": {}, "zones": {}, "px_per_m": px_per_m}

    ox = origin["x"] + origin["w"] / 2
    oy = origin["y"] + origin["h"] / 2
    ogx = round((ox - vb["x"]) / _GS - 0.5)
    ogy = round((oy - vb["y"]) / _GS - 0.5)

    seed = -1
    for r in range(25):
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                gx, gy = ogx + dx, ogy + dy
                if 0 <= gx < cols and 0 <= gy < rows and walk[gy * cols + gx]:
                    seed = gy * cols + gx
                    break
            if seed >= 0:
                break
        if seed >= 0:
            break

    # 走道上的最短路：8 方向 Dijkstra（直走 1、斜走 √2）。
    # 若只用四方向 BFS，算出來的是曼哈頓距離，等高線是菱形，而且對角線上會有大量
    # 「距離完全相同」的格子——切 A/B 等級時只能任意切開那一群，畫面就出現紅橙棋盤格。
    # 改成 8 方向後距離接近真實步行距離，等高線近似同心圓，同距離的格子也大幅減少。
    INF = float("inf")
    dist = [INF] * (cols * rows)
    if seed >= 0:
        dist[seed] = 0.0
        pq = [(0.0, seed)]
    else:
        pq = []
    _R2 = math.sqrt(2)
    _STEPS = ((1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
              (1, 1, _R2), (1, -1, _R2), (-1, 1, _R2), (-1, -1, _R2))
    while pq:
        d, cur = heapq.heappop(pq)
        if d > dist[cur]:
            continue
        cgx, cgy = cur % cols, cur // cols
        cx, cy = nx(cgx), ny(cgy)
        for dx, dy, w in _STEPS:
            ax, ay = cgx + dx, cgy + dy
            if not (0 <= ax < cols and 0 <= ay < rows):
                continue
            ai = ay * cols + ax
            if not walk[ai]:
                continue
            if dy != 0 and not _cross_ok(cx, cy, ny(ay)):
                continue
            # 斜走時不允許「穿過兩個障礙物的夾角」，否則會抄到實際上走不通的捷徑
            if dx != 0 and dy != 0 and not (walk[cgy * cols + ax] or walk[ay * cols + cgx]):
                continue
            nd = d + w
            if nd < dist[ai]:
                dist[ai] = nd
                heapq.heappush(pq, (nd, ai))

    # 每個分區的每個代理格 → 繞行距離（公尺）
    #
    # 每一格各自找「離自己最近的可走網格點」，取該點的 BFS 步數再加上走到該格的短距離。
    # 前端原本的做法是整個分區共用一個入口點（zbase）再加直線距離，等於把等高線畫成
    # 「以該區入口為圓心的同心圓」——每個分區各有自己的圓心，於是熱度色帶在分區之間
    # 對不起來、看起來像一區一個弧。改成逐格計算後，等高線會沿著走道從出貨點整體向外擴散。
    # 把走道上的 BFS 距離往「不可走的格」內部擴散一次（多源 BFS）：
    # 大型分區（例如 L4 有 1085 格、R4 有 1425 格）的內部離任何走道都很遠，逐格往外找
    # 最近走道點會找不到；改用一次性的多源擴散，每一格都必然有值，且值是連續的
    # 「沿走道走到該區旁邊 ＋ 再走進去幾格」，等高線因此能平順地從出貨點向外擴散。
    field = list(dist)
    pq2 = [(v, i) for i, v in enumerate(field) if v < INF]
    heapq.heapify(pq2)
    while pq2:
        d, cur = heapq.heappop(pq2)
        if d > field[cur]:
            continue
        cgx, cgy = cur % cols, cur // cols
        for dx, dy, w in _STEPS:
            ax, ay = cgx + dx, cgy + dy
            if not (0 <= ax < cols and 0 <= ay < rows):
                continue
            ai = ay * cols + ax
            if walk[ai]:
                continue          # 走道本身的距離已由第一階段算好
            nd = d + w
            if nd < field[ai]:
                field[ai] = nd
                heapq.heappush(pq2, (nd, ai))

    def cell_dist(cx, cy):
        gx = max(0, min(cols - 1, round((cx - vb["x"]) / _GS - 0.5)))
        gy = max(0, min(rows - 1, round((cy - vb["y"]) / _GS - 0.5)))
        v = field[gy * cols + gx]
        return float(v) if v < INF else 9999.0

    cells = {}
    zone_mean = {}
    for z in geo["zones"]:
        zc = z.get("cells") or []
        if not zc:
            continue
        arr = []
        for c in zc:
            cx = z["x"] + c[0] + c[2] / 2
            cy = z["y"] + c[1] + c[3] / 2
            arr.append(cell_dist(cx, cy) * _GS / px_per_m)           # 網格單位 → px → 公尺
        cells[z["code"]] = arr
        zone_mean[z["code"]] = sum(arr) / len(arr)

    return {"cells": cells, "zones": zone_mean, "px_per_m": px_per_m}


def cell_distances():
    """{分區代號: [每格繞行距離(公尺), ...]}，順序與幾何檔的 cells 相同。結果快取於記憶體。"""
    global _CACHE
    if _CACHE is None:
        with _LOCK:
            if _CACHE is None:
                _CACHE = _compute()
    return _CACHE["cells"]


def zone_mean_distances():
    """{分區代號: 該區所有格子的平均繞行距離(公尺)}。"""
    global _CACHE
    if _CACHE is None:
        cell_distances()
    return _CACHE["zones"]
