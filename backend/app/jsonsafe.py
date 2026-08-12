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
    2026-08 隨新增的儲位配置精細版／批次地理分群端點一併補上）。"""
    if isinstance(k, np.integer):
        return int(k)
    if isinstance(k, np.floating):
        return float(k)
    if isinstance(k, np.bool_):
        return bool(k)
    if isinstance(k, (pd.Timestamp,)):
        return k.strftime("%Y-%m-%d")
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
