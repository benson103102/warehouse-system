# -*- coding: utf-8 -*-
"""離線路名＋村里 回推/驗證的回歸測試（純本地、不連外）。

執行：
    python backend/tests/test_address_reference.py
或從 backend 目錄：
    python tests/test_address_reference.py

驗證範圍：
  1. 名冊載入（路名 + 村里 兩份隨附 gz）。
  2. 路名/村里 擷取（去段別、去村里前綴、台/臺 正規化）。
  3. resolve_region 三種回推：路名∩村里交集、路名全國唯一、村里全國唯一，及無法唯一→None。
  4. verify：縣市+區域+路名存在於名冊才 True。
  5. resolve_addresses_offline 整合：待確認被補齊、已判出者驗證、未載入名冊時 no-op。
"""
import os
import sys
import tempfile

os.environ["WAREHOUSE_DATA_DIR"] = tempfile.mkdtemp(prefix="addr_ref_test_")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pandas as pd  # noqa: E402

from app.services import address_reference as ar  # noqa: E402
from app.services import cleaning_core as cc  # noqa: E402

_ok = _fail = 0


def check(name, cond):
    global _ok, _fail
    if cond:
        _ok += 1
        print(f"  PASS: {name}")
    else:
        _fail += 1
        print(f"  FAIL: {name} (got: {cond!r})")


def test_load():
    print("== 名冊載入（路名 + 村里）==")
    ar.load(force=True)
    check("名冊已載入", ar.is_loaded() is True)
    s = ar.stats()
    check("有路名唯一索引", s.get("road_names_unique", 0) > 10000)
    check("有村里唯一索引", s.get("village_names_unique", 0) > 3000)
    print(f"    stats: {s}")


def test_extract():
    print("== 路名/村里 擷取（輸入為去縣市/區域後的 rest）==")
    check("去段別：忠孝東路四段561號→忠孝東路", ar._extract_road("忠孝東路四段561號") == "忠孝東路")
    check("去村里前綴取路名：光榮里光榮街90號→光榮街", ar._extract_road("光榮里光榮街90號") == "光榮街")
    check("擷取村里：光榮里光榮街90號→光榮里", ar._extract_village("光榮里光榮街90號") == "光榮里")
    check("無村里→None", ar._extract_village("士東路100號") is None)
    check("純號碼無路名→None", ar._extract_road("100號") is None)


def test_resolve_logic():
    """用受控的小索引精準驗證 resolve_region 三種回推路徑（不依賴特定真實地名）。"""
    print("== resolve_region 三種回推路徑（受控索引）==")
    ar.load(force=True)
    # 備份真實索引，換上受控資料，測完還原
    bak_r, bak_v, bak_loaded = ar._road_regions, ar._village_regions, ar._loaded
    try:
        ar._loaded = True
        # 中山路遍布 A區/B區/C區；光復里在 B區/D區；交集只剩 B區
        ar._road_regions = {
            "中山路": frozenset({"甲市A區", "甲市B區", "乙市C區"}),
            "獨有路": frozenset({"丙市X區"}),
            "共用路": frozenset({"甲市A區", "乙市C區"}),
        }
        ar._village_regions = {
            "光復里": frozenset({"甲市B區", "丁市D區"}),
            "獨有里": frozenset({"戊市Y區"}),
            "共用里": frozenset({"丁市D區", "戊市Y區"}),  # 與「共用路」交集為空
        }
        check("A. 路名∩村里交集唯一：中山路+光復里→甲市B區",
              ar.resolve_region("光復里中山路5號") == "甲市B區")
        check("B. 路名全國唯一：獨有路→丙市X區", ar.resolve_region("獨有路1號") == "丙市X區")
        check("C. 村里全國唯一：獨有里→戊市Y區", ar.resolve_region("獨有里9號") == "戊市Y區")
        check("路名不唯一且無村里→None", ar.resolve_region("中山路5號") is None)
        check("路名+村里皆不唯一且交集非單一→None", ar.resolve_region("共用里共用路7號") is None)
    finally:
        ar._road_regions, ar._village_regions, ar._loaded = bak_r, bak_v, bak_loaded


def test_real_unique_road():
    print("== 真實資料：唯一路名/驗證 ==")
    ar.load(force=True)
    check("唯一路名『士東路』回推=台北市士林區", ar.resolve_region("士東路100號") == "台北市士林區")
    check("常見路名『中山路』(無村里)不回推", ar.resolve_region("中山路1號") is None)
    check("verify 台北市士林區士東路=True", ar.verify("台北市", "士林區", "士東路5號") is True)
    check("verify 錯區=False", ar.verify("台北市", "信義區", "士東路5號") is False)


def test_integration():
    print("== resolve_addresses_offline 整合 ==")
    ar.load(force=True)

    def mkrow(rid, addr, region, pending, code="C1"):
        return {
            "_原始列號": rid, cc.ADDRESS_COLUMN: addr, cc.STORE_CODE_COLUMN: code,
            "系統出貨單號": "OU2024070100000%d" % rid,
            "配送地址_補值來源": "同門市代碼單一可信版本回填",
            "配送地址_待確認_flag": pending, "配送區域": region,
        }

    clean_df = pd.DataFrame([
        mkrow(2, "士東路100號", None, 1),              # 待確認→唯一路名回推補齊
        mkrow(3, "台北市士林區士東路5號", "台北市士林區", 0),  # 已判出→路名相符(已驗證)
    ])
    iso_df = pd.DataFrame([
        mkrow(4, "中山路1號", None, 1),                # 待確認 + 常見路名無村里→仍待確認
    ])
    new_clean, new_iso, _ = cc.resolve_addresses_offline(clean_df, iso_df, [])

    r2 = new_clean[new_clean["_原始列號"] == 2].iloc[0]
    check("待確認被補齊=台北市士林區", r2["配送區域"] == "台北市士林區")
    check("待確認 flag 解除", r2["配送地址_待確認_flag"] == 0)
    check("比對='路名回推補齊'、來源含全國路名資料回推",
          r2["配送地址_離線比對"] == "路名回推補齊" and "全國路名資料回推" in r2["配送地址_補值來源"])

    r3 = new_clean[new_clean["_原始列號"] == 3].iloc[0]
    check("已判出者標『路名相符(已驗證)』", r3["配送地址_離線比對"] == "路名相符(已驗證)")

    r4 = new_iso[new_iso["_原始列號"] == 4].iloc[0]
    check("常見路名維持待確認、比對='未比對'",
          r4["配送地址_待確認_flag"] == 1 and r4["配送地址_離線比對"] == "未比對")


def test_no_op_when_missing():
    print("== 兩份名冊都缺時 no-op ==")
    miss = os.path.join(tempfile.gettempdir(), "definitely_missing.csv")
    os.environ["WAREHOUSE_ROAD_DATA"] = miss
    os.environ["WAREHOUSE_VILLAGE_DATA"] = miss
    ar.load(force=True)
    try:
        check("名冊都不存在→is_loaded False", ar.is_loaded() is False)
        df = pd.DataFrame([{
            "_原始列號": 2, cc.ADDRESS_COLUMN: "士東路100號", cc.STORE_CODE_COLUMN: "C1",
            "系統出貨單號": "OU202407010000002", "配送地址_補值來源": "x",
            "配送地址_待確認_flag": 1, "配送區域": None,
        }])
        new_clean, _, _ = cc.resolve_addresses_offline(df, df.iloc[0:0].copy(), [])
        r = new_clean.iloc[0]
        check("未載入時比對='未載入參考資料'", r["配送地址_離線比對"] == "未載入參考資料")
        check("未載入時維持待確認", r["配送地址_待確認_flag"] == 1)
    finally:
        os.environ.pop("WAREHOUSE_ROAD_DATA", None)
        os.environ.pop("WAREHOUSE_VILLAGE_DATA", None)
        ar.load(force=True)


if __name__ == "__main__":
    test_load()
    test_extract()
    test_resolve_logic()
    test_real_unique_road()
    test_integration()
    test_no_op_when_missing()
    print(f"\n==== 結果：{_ok} passed, {_fail} failed ====")
    sys.exit(1 if _fail else 0)
