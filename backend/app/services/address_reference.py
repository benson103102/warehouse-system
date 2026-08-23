"""離線地址參考名冊（內政部「全國路名資料」＋「村里清單」）— 供配送地址離線回推/驗證。

用途
----
配送地址補值原本完全靠 cleaning_core 的規則推測。本模組提供**離線**的官方路名＋村里
名冊，讓規則判不出縣市/區域的殘缺地址，能靠「路名／村里→縣市+區域」的全國唯一性
（以及「路名∩村里」交集）回推補齊，並對已判出的地址做正向驗證。完全不連外、100%
決定性、零個資外洩。

資料來源（皆為政府資料開放授權條款第1版，可自由使用/散布，已隨專案打包快照）
----------------------------------------------------------------------------
  1. 路名：內政部戶政司「全國路名資料」（data.gov.tw dataset 35321 / ODRP049）。
     欄位 city／site_id(縣市+區域)／road。快照 `reference/road_names_113.csv.gz`。
     可用環境變數 WAREHOUSE_ROAD_DATA 指向更新版（支援 .csv 與 .csv.gz）。
  2. 村里：內政部國土測繪中心 NLSC ListCounty/ListTown/ListVillage API 彙整。
     欄位 city／district／village。快照 `reference/village_names.csv.gz`。
     可用環境變數 WAREHOUSE_VILLAGE_DATA 指向更新版。
兩份任一存在即可運作；兩份都缺才視為未載入。

回推策略（高精確度，受限於名冊為「非窮舉」清單，不做「查無即判錯」以免大量誤報）
--------------------------------------------------------------------------------
規則判『待確認』（缺縣市/區域）時，依序嘗試：
  A. 路名∩村里 交集：地址同時有路名與村里時，取兩者各自對應的 (縣市,區域) 集合交集，
     若交集恰為單一 → 回推。這能救回「路名不唯一（中山路遍地都是）、村里也不唯一，
     但兩者組合起來全國唯一」的一大批地址，是加入村里資料最主要的效益。
  B. 路名全國唯一 → 回推。
  C. 村里全國唯一 → 回推。
其餘一律「未比對」，不改動規則結果。
驗證：地址已有縣市+區域，且該「縣市+區域+路名」存在於路名名冊 → 標記「路名相符(已驗證)」。

台／臺 正規化
-------------
名冊用官方「臺」字，cleaning_core 管線多用俗寫「台」字。索引一律正規化為「台」字後
建立與查詢，確保回推出的配送區域與管線其他 key 一致（否則併單分組會被「台北市」vs
「臺北市」拆成兩組）。
"""

import csv
import gzip
import os
import re

_HERE = os.path.dirname(__file__)
_DEFAULT_ROAD_DATA = os.path.join(_HERE, "reference", "road_names_113.csv.gz")
_DEFAULT_VILLAGE_DATA = os.path.join(_HERE, "reference", "village_names.csv.gz")

# 擷取「路名」用：輸入是**已去除縣市/區域的剩餘文字**（路名+門牌，由呼叫端先用
# split_city_district_rest 拆好），這樣才不會把「臺北市士林區」這類前綴一起吃進路名
# （中文地址無分隔符，若對完整地址做非貪婪比對，會從最前面一路吃到第一個 路/街/道，
#  例：臺北市士林區士東路 → 誤取「臺北市士林區士東路」）。
# 名冊的 road 欄不含段別（例「文化路」而非「文化路一段」），故擷取到的路名要去掉尾端
# 「N段」再比對。允許路名含中文數字（例：海洋一路、忠孝東路）。取「最後一個」路名詞。
_ROAD_TOKEN_RE = re.compile(r"([一-鿿]{1,10}?(?:大道|大街|路|街|道))")
_SECTION_SUFFIX_RE = re.compile(r"[一二三四五六七八九十\d]+段$")
# 村里：取開頭（去縣市/區域後，村里通常在最前面）結尾為 村/里 的詞。
_VILLAGE_TOKEN_RE = re.compile(r"^([一-鿿]{1,8}?[村里])")

# 模組層快取：載入一次後重複使用（清洗一次可能查數十萬地址，不能每次重讀檔）。
_loaded = False
_road_regions = {}     # road_canon -> frozenset of "縣市區域"（全部，供交集用）
_village_regions = {}  # village_canon -> frozenset of "縣市區域"（全部，供交集用）
_valid_triples = set()  # (city_canon, district, road_canon)，供正向驗證


def _canon(s):
    """台／臺 正規化（統一為管線慣用的「台」字）。"""
    return s.replace("臺", "台") if isinstance(s, str) else s


def _open_maybe_gzip(path):
    if path.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", newline="")
    return open(path, "rt", encoding="utf-8", newline="")


def _road_data_path():
    return os.environ.get("WAREHOUSE_ROAD_DATA", "").strip() or _DEFAULT_ROAD_DATA


def _village_data_path():
    return os.environ.get("WAREHOUSE_VILLAGE_DATA", "").strip() or _DEFAULT_VILLAGE_DATA


def _read_csv_rows(path):
    """讀 CSV（可 gzip），回傳 (fieldnames_lower_map, list_of_row_dicts)；失敗回 (None, [])。"""
    if not path or not os.path.exists(path):
        return None, []
    try:
        with _open_maybe_gzip(path) as f:
            reader = csv.DictReader(f)
            fields = {str(c).strip().lower(): c for c in (reader.fieldnames or [])}
            return fields, list(reader)
    except Exception:  # noqa: BLE001 — 解析失敗當作無此名冊，不拖垮清洗
        return None, []


def _load_roads():
    """→ (road_regions dict[str,set], valid_triples set)。"""
    fields, rows = _read_csv_rows(_road_data_path())
    if not fields:
        return {}, set()
    c_city, c_site, c_road = fields.get("city"), fields.get("site_id"), fields.get("road")
    if not (c_city and c_site and c_road):
        return {}, set()
    road_to_regions = {}
    triples = set()
    for row in rows:
        city = _canon((row.get(c_city) or "").strip())
        site = _canon((row.get(c_site) or "").strip())
        road = _canon((row.get(c_road) or "").strip())
        if not (city and site and road):
            continue
        district = site[len(city):] if site.startswith(city) else site
        if not district:
            continue
        road_to_regions.setdefault(road, set()).add(city + district)
        triples.add((city, district, road))
    return road_to_regions, triples


def _load_villages():
    """→ village_regions dict[str,set]。CSV 欄位 city／district／village
    （或 city／site_id／village；site_id=縣市+區域）。"""
    fields, rows = _read_csv_rows(_village_data_path())
    if not fields:
        return {}
    c_city = fields.get("city")
    c_vil = fields.get("village")
    c_dist = fields.get("district")
    c_site = fields.get("site_id")
    if not (c_city and c_vil and (c_dist or c_site)):
        return {}
    vil_to_regions = {}
    for row in rows:
        city = _canon((row.get(c_city) or "").strip())
        vil = _canon((row.get(c_vil) or "").strip())
        if c_dist:
            district = _canon((row.get(c_dist) or "").strip())
        else:
            site = _canon((row.get(c_site) or "").strip())
            district = site[len(city):] if site.startswith(city) else site
        if not (city and district and vil):
            continue
        vil_to_regions.setdefault(vil, set()).add(city + district)
    return vil_to_regions


def load(force: bool = False):
    """載入並建立索引（冪等；已載入則直接返回，除非 force=True 重載）。
    兩份名冊皆不存在/解析失敗時，維持「未載入」狀態（is_loaded()=False），呼叫端據此略過。"""
    global _loaded, _road_regions, _village_regions, _valid_triples
    if _loaded and not force:
        return
    road_regions, triples = _load_roads()
    village_regions = _load_villages()
    if not road_regions and not village_regions:
        _loaded = False
        return
    _road_regions = {r: frozenset(s) for r, s in road_regions.items()}
    _village_regions = {v: frozenset(s) for v, s in village_regions.items()}
    _valid_triples = triples
    _loaded = True


def is_loaded() -> bool:
    if not _loaded:
        load()
    return _loaded


def _extract_road(rest: str):
    """從『已去除縣市/區域的剩餘文字』擷取路名（去村里前綴、去段別、台/臺正規化）。找不到回 None。"""
    if not isinstance(rest, str) or not rest:
        return None
    text = _VILLAGE_TOKEN_RE.sub("", rest.strip())  # 去掉開頭村里（若有）
    matches = _ROAD_TOKEN_RE.findall(text)
    if not matches:
        return None
    road = _SECTION_SUFFIX_RE.sub("", matches[-1])
    return _canon(road) if road else None


def _extract_village(rest: str):
    """從『已去除縣市/區域的剩餘文字』擷取開頭的村里名（台/臺正規化）。找不到回 None。"""
    if not isinstance(rest, str) or not rest:
        return None
    m = _VILLAGE_TOKEN_RE.match(rest.strip())
    return _canon(m.group(1)) if m else None


def resolve_region(rest: str):
    """地址缺縣市/區域時：傳入去除縣市/區域後的剩餘文字（路名+門牌）。依序用
    路名∩村里交集 → 路名全國唯一 → 村里全國唯一 回推「縣市區域」字串（台/臺正規化）；
    都無法唯一決定則回 None。"""
    if not is_loaded():
        return None
    road = _extract_road(rest)
    village = _extract_village(rest)
    road_set = _road_regions.get(road) if road else None
    vil_set = _village_regions.get(village) if village else None

    # A. 交集（路名與村里同時可查時最精準）
    if road_set and vil_set:
        inter = road_set & vil_set
        if len(inter) == 1:
            return next(iter(inter))
    # B. 路名全國唯一
    if road_set and len(road_set) == 1:
        return next(iter(road_set))
    # C. 村里全國唯一
    if vil_set and len(vil_set) == 1:
        return next(iter(vil_set))
    return None


def verify(city: str, district: str, rest: str) -> bool:
    """驗證：地址的「縣市+區域+路名」是否存在於路名名冊（正向確認；查無不代表地址錯）。
    rest 為去除縣市/區域後的剩餘文字（路名+門牌）。"""
    if not is_loaded():
        return False
    road = _extract_road(rest)
    if not road:
        return False
    return (_canon(city), _canon(district), road) in _valid_triples


def stats() -> dict:
    """名冊概況（供除錯/摘要）。"""
    if not is_loaded():
        return {"loaded": False, "road_path": _road_data_path(), "village_path": _village_data_path()}
    uniq_road = sum(1 for s in _road_regions.values() if len(s) == 1)
    uniq_vil = sum(1 for s in _village_regions.values() if len(s) == 1)
    return {
        "loaded": True,
        "road_names": len(_road_regions),
        "road_names_unique": uniq_road,
        "village_names": len(_village_regions),
        "village_names_unique": uniq_vil,
        "valid_triples": len(_valid_triples),
    }
