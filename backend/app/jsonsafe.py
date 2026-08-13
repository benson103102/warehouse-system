"""
把 pandas / numpy 運算結果轉成能被 JSON 序列化的原生 Python 型別。

分析函式內部大量使用 pandas Series／numpy 純量（np.int64／np.float64／NaN／
pd.Timestamp），這些型別不能直接被標準 json 模組序列化，FastAPI 底層雖然會
用 jsonable_encoder 幫忙轉換，但遇到 NaN／Infinity 或自訂物件時容易出非預期
結果。統一在路由回傳前呼叫 clean()，明確控制轉換規則，行為可預期、好除錯。
"""

import math

import numpy as np
import pandas as pd


def _clean_key(k):
    """字典的 key 也可能是 numpy 純量（例如以 pandas groupby 索引值當 key，如
    before_cat_zone／after_sku_zone／zone_before_cats 這類「SKU/類別ID → ...」的結果），
    標準 json 模組只認得 str/int/float/bool/None 當 key，遇到 numpy.int64 等型別會直接
    噴錯，所以 key 也要走一次型別轉換（對應原本只轉換 value、沒轉換 key 的既有行為，
    2026-08 隨新增的儲位配置精細版／批次地理分群端點一併補上）。

    這裡的 key 目前全是「ID 類」的整數值（SKU ID、商品類別代碼…），但因為清洗流程中
    有些欄位（例如商品類別）曾與缺值共存過，pandas 會把整欄upcast成 float64，導致這些
    ID 讀出來變成 np.float64(15.0) 而非整數。若不處理，json.dumps 對 float key 15.0 會
    輸出 "15.0"（帶小數點的字串），但同一個值若是當成一般欄位值（例如 info.cat）序列化，
    會輸出數字 15.0 → 前端 JSON.parse 後是數字 15（JS 數字不分整數/浮點）。前端用這個
    數字去查以 ID 當 key 的字典（例如 before_cat_zone[r.info.cat]）時，會變成用 "15" 去
    查 "15.0" 這個 key，永遠查不到 → 側邊圖表看似正常但「改善前」的精準定位/反白會靜默
    失敗（無錯誤訊息，只是查不到而已）。因此這裡對「整數值的 float」一律轉成 int，讓
    key 的 JSON 字串跟這個值當成一般欄位時的字串（去掉小數點後）能夠對得上。"""
    if isinstance(k, np.integer):
        return int(k)
    if isinstance(k, np.floating):
        f = float(k)
        return int(f) if f.is_integer() else f
    if isinstance(k, np.bool_):
        return bool(k)
    if isinstance(k, (pd.Timestamp,)):
        return k.strftime("%Y-%m-%d")
    if isinstance(k, float) and k.is_integer():
        return int(k)
    return k


def clean(obj):
    if obj is None:
        return None
    if isinstance(obj, dict):
        return {_clean_key(k): clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [clean(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        v = float(obj)
        return None if (math.isnan(v) or math.isinf(v)) else v
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, (pd.Timestamp,)):
        if pd.isna(obj):
            return None
        return obj.strftime("%Y-%m-%d")
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, (np.ndarray,)):
        return clean(obj.tolist())
    try:
        if pd.isna(obj):
            return None
    except (TypeError, ValueError):
        pass
    return obj
