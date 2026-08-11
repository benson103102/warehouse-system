"""
Session 狀態管理（落地保存版）。

原前端把狀態放在瀏覽器分頁的 JS 全域變數，重新整理就全部消失、也無法給其他人共用。
第一版後端把它改成「一個進程內的記憶體字典」——能動，但重啟就遺失、且大檔整份佔記憶體。

這一版把狀態改成落地保存（見 store.py），達成原本註解列出的三個目標：
  - 重啟不遺失：狀態存 SQLite + 磁碟，伺服器重開後自動載回。
  - 多人隔離：以 session id 當 key，各自獨立的資料夾與資料表列，互不干擾。
  - 大檔不常駐記憶體：raw_df／清洗結果採「延遲載入」，第一次真的用到才從磁碟讀進來，
    平時 session 只帶著輕量的中繼資料（狀態、檔名）。

對呼叫端（routers）而言，介面幾乎不變：一樣是 get_session(sid) 取得 SessionState、
一樣讀寫 sess.raw_df／sess.cleaning_result。差別只在「寫入後要呼叫對應的 persist_*
把結果落地」——這一步刻意做成明確呼叫（而非在 setter 裡偷偷寫檔），因為寫百萬列
DataFrame 是很重的 I/O，明擺出來比較好讀、好抓效能。

仍未做、正式系統要再補的部分：登入驗證（目前 session id 由前端自帶，尚未綁定帳號）、
把上傳檔搬到物件儲存（S3／R2）而非本機磁碟、清洗結果過期清理。
"""

import json
import os
import threading

from . import store

DEFAULT_SESSION_ID = "default"

_LOCK = threading.Lock()
_SESSIONS: dict = {}

_ZONES_PATH = os.path.join(os.path.dirname(__file__), "services", "zones_config.json")
with open(_ZONES_PATH, encoding="utf-8") as _f:
    _DEFAULT_ZONES = json.load(_f)

store.init_db()


class SessionState:
    """單一 session 的狀態。raw_df／cleaning_result 為延遲載入屬性：
    記憶體沒有、但磁碟上有（has_raw／has_clean）時，第一次存取才從 store 讀回並快取。"""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.raw_filename = None
        self.zones = list(_DEFAULT_ZONES)
        self.status = "idle"
        self.load_error = None
        self._has_raw = False
        self._has_clean = False
        self._raw_df = None
        self._raw_loaded = False
        self._cleaning_result = None
        self._clean_loaded = False
        # 分析中間結果快取（記憶體、不落地）：昂貴且與篩選參數無關的計算（SKU頻次、
        # 需求預測聚合、批次模擬…）算一次就存起來，之後切換篩選／拉滑桿直接重用，維持互動即時。
        # 清洗結果一換（cleaning_result setter／clear_clean）整份快取自動失效。詳見 cached()。
        self._analytics_cache = {}

    @property
    def raw_df(self):
        if self._raw_df is None and self._has_raw and not self._raw_loaded:
            self._raw_df = store.load_raw_df(self.session_id)
            self._raw_loaded = True
        return self._raw_df

    @raw_df.setter
    def raw_df(self, value):
        self._raw_df = value
        self._raw_loaded = True

    @property
    def cleaning_result(self):
        if self._cleaning_result is None and self._has_clean and not self._clean_loaded:
            self._cleaning_result = store.load_clean_result(self.session_id)
            self._clean_loaded = True
        return self._cleaning_result

    @cleaning_result.setter
    def cleaning_result(self, value):
        self._cleaning_result = value
        self._clean_loaded = True
        self._analytics_cache = {}   # 新的清洗結果 → 既有分析快取全部失效

    def cached(self, key, compute):
        """把昂貴、與篩選參數無關的中間結果快取在 session（記憶體）。

        key 要包含「這個結果真正依賴的輸入」——例如預測聚合依賴粒度就用 ("fc_agg", granularity)、
        批次模擬依賴容量就用 ("batch", capacity)。改到那些輸入才會 cache miss、重算；改其他參數
        （切 SKU、切 qty/cnt、拉工時／速度滑桿…）都 cache hit，直接重用，維持互動即時。
        清洗結果一換，整份快取自動清空（見上方 setter 與 clear_clean）。"""
        c = self._analytics_cache
        if key not in c:
            c[key] = compute()
        return c[key]


def get_session(session_id: str = DEFAULT_SESSION_ID) -> SessionState:
    """取得 session：記憶體有就直接回；沒有則從 store 載回中繼資料（首見則建立並落地）。"""
    sid = store.sanitize_sid(session_id or DEFAULT_SESSION_ID)
    with _LOCK:
        if sid in _SESSIONS:
            return _SESSIONS[sid]
        meta = store.load_session_meta(sid)
        sess = SessionState(sid)
        if meta is None:
            store.save_session_meta(sid, None, "idle", None, 0, 0)
        else:
            sess.raw_filename = meta["raw_filename"]
            sess.status = meta["status"]
            sess.load_error = meta["load_error"]
            sess._has_raw = bool(meta["has_raw"])
            sess._has_clean = bool(meta["has_clean"])
        _SESSIONS[sid] = sess
        return sess


def reset_session(session_id: str = DEFAULT_SESSION_ID) -> None:
    sid = store.sanitize_sid(session_id or DEFAULT_SESSION_ID)
    with _LOCK:
        store.delete_raw(sid)
        store.delete_clean(sid)
        store.save_session_meta(sid, None, "idle", None, 0, 0)
        _SESSIONS[sid] = SessionState(sid)


# ---- 落地保存輔助：routers 在改動 session 後呼叫，把結果寫回磁碟／SQLite ----

def persist_meta(sess: SessionState) -> None:
    store.save_session_meta(
        sess.session_id, sess.raw_filename, sess.status, sess.load_error,
        int(sess._has_raw), int(sess._has_clean),
    )


def persist_raw(sess: SessionState) -> None:
    """把 sess.raw_df 落地（解析後的原始 DataFrame），並更新中繼資料。"""
    store.save_raw_df(sess.session_id, sess._raw_df)
    sess._has_raw = True
    persist_meta(sess)


def clear_raw(sess: SessionState) -> None:
    store.delete_raw(sess.session_id)
    sess._raw_df = None
    sess._raw_loaded = True
    sess._has_raw = False


def persist_clean(sess: SessionState) -> None:
    """把清洗結果落地（CleaningResult），並更新中繼資料。"""
    store.save_clean_result(sess.session_id, sess._cleaning_result)
    sess._has_clean = True
    persist_meta(sess)


def clear_clean(sess: SessionState) -> None:
    store.delete_clean(sess.session_id)
    sess._cleaning_result = None
    sess._clean_loaded = True
    sess._has_clean = False
    sess._analytics_cache = {}   # 清洗結果沒了 → 分析快取一併清空
