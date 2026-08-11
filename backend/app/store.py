"""
落地保存層（把原本只存在記憶體的 session 狀態改成存到磁碟）。

原本 state.py 的說明就寫著「正式系統應該改成：用資料庫存 session（多人多裝置共用、
重啟不遺失）、大型資料集不要整份存記憶體」。這支模組就是那一步，但刻意只用 Python
標準函式庫（sqlite3 + pickle），不引入任何新套件，方便你在自己機器或雲主機直接跑。

保存策略：
  - SQLite（data/warehouse.db）：只存「小而結構化」的中繼資料
      · sessions 表：每個 session 目前的狀態（檔名／status／是否有原始資料／是否已清洗）
      · uploads  表：每一次上傳的檔案紀錄（檔名、實際存放路徑、大小、列數、時間）
                     —— 這就是「系統能記錄每位使用者上傳過哪些檔案」的地方
  - 磁碟檔：存「大而非結構化」的資料
      · data/sessions/{sid}/raw.pkl    使用者上傳、解析後的原始 DataFrame
      · data/sessions/{sid}/clean.pkl  清洗結果（CleaningResult，內含 5 個 DataFrame）
      · data/uploads/{sid}/{時間}_{原檔名}  使用者上傳的「原始檔位元組」原封不動保存一份

為什麼用 pickle 而不是 parquet：這些 DataFrame 大量使用 dtype=object（同一欄混型別），
parquet 對這種欄位容易報錯或悄悄改型別；pickle 能原封不動存回、且不需額外套件。
注意：pickle 反序列化等同執行程式碼，因此這裡「只」讀取本服務自己寫出、放在自己 data
目錄下的檔案，絕不 unpickle 外部來源資料。

多執行緒：ingest 的實際讀檔跑在背景執行緒，所以每次操作都開一條新的 sqlite 連線
（sqlite3 連線不可跨執行緒共用），並開 WAL 模式降低讀寫互鎖。
"""

import os
import pickle
import re
import sqlite3
import threading
from datetime import datetime, timezone

import pandas as pd

_HERE = os.path.dirname(__file__)
DATA_DIR = os.environ.get("WAREHOUSE_DATA_DIR", os.path.join(_HERE, "..", "data"))
DATA_DIR = os.path.abspath(DATA_DIR)
_DB_PATH = os.path.join(DATA_DIR, "warehouse.db")
_SESSIONS_DIR = os.path.join(DATA_DIR, "sessions")
_UPLOADS_DIR = os.path.join(DATA_DIR, "uploads")

_INIT_LOCK = threading.Lock()
_initialized = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db() -> None:
    """建立資料夾與資料表。冪等，可重複呼叫。"""
    global _initialized
    with _INIT_LOCK:
        if _initialized:
            return
        os.makedirs(_SESSIONS_DIR, exist_ok=True)
        os.makedirs(_UPLOADS_DIR, exist_ok=True)
        with _connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id   TEXT PRIMARY KEY,
                    raw_filename TEXT,
                    status       TEXT NOT NULL DEFAULT 'idle',
                    load_error   TEXT,
                    has_raw      INTEGER NOT NULL DEFAULT 0,
                    has_clean    INTEGER NOT NULL DEFAULT 0,
                    created_at   TEXT NOT NULL,
                    updated_at   TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS uploads (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id  TEXT NOT NULL,
                    filename    TEXT NOT NULL,
                    stored_path TEXT,
                    size_bytes  INTEGER,
                    row_count   INTEGER,
                    status      TEXT NOT NULL DEFAULT 'processing',
                    error       TEXT,
                    uploaded_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_uploads_session
                    ON uploads(session_id, id DESC);

                CREATE TABLE IF NOT EXISTS users (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    email         TEXT UNIQUE NOT NULL COLLATE NOCASE,
                    password_hash TEXT NOT NULL,
                    created_at    TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS auth_tokens (
                    token      TEXT PRIMARY KEY,
                    user_id    INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tokens_user
                    ON auth_tokens(user_id);
                """
            )
        _initialized = True


# --------------------------------------------------------------------------
# session id 淨化：header 帶進來的字串會被拿去當資料夾名稱，必須防目錄穿越。
# 允許的安全字元原樣保留（方便除錯時肉眼對照），其餘一律以 sha256 雜湊取代。
# --------------------------------------------------------------------------
_SAFE_SID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def sanitize_sid(session_id: str) -> str:
    if session_id and _SAFE_SID.match(session_id):
        return session_id
    import hashlib

    return "h_" + hashlib.sha256((session_id or "").encode("utf-8")).hexdigest()[:32]


def _session_dir(sid: str) -> str:
    d = os.path.join(_SESSIONS_DIR, sid)
    os.makedirs(d, exist_ok=True)
    return d


def _uploads_dir(sid: str) -> str:
    d = os.path.join(_UPLOADS_DIR, sid)
    os.makedirs(d, exist_ok=True)
    return d


def _raw_path(sid: str) -> str:
    return os.path.join(_session_dir(sid), "raw.pkl")


def _clean_path(sid: str) -> str:
    return os.path.join(_session_dir(sid), "clean.pkl")


# ------------------------------ sessions ----------------------------------

def load_session_meta(sid: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (sid,)
        ).fetchone()
    return dict(row) if row else None


def save_session_meta(sid: str, raw_filename, status: str, load_error,
                      has_raw: int, has_clean: int) -> None:
    now = _now()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO sessions
                (session_id, raw_filename, status, load_error, has_raw, has_clean,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                raw_filename = excluded.raw_filename,
                status       = excluded.status,
                load_error   = excluded.load_error,
                has_raw      = excluded.has_raw,
                has_clean    = excluded.has_clean,
                updated_at   = excluded.updated_at
            """,
            (sid, raw_filename, status, load_error, has_raw, has_clean, now, now),
        )


def list_sessions() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM sessions ORDER BY updated_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


# ------------------------------ raw / clean 大檔 ---------------------------

def save_raw_df(sid: str, df: pd.DataFrame) -> None:
    df.to_pickle(_raw_path(sid))


def load_raw_df(sid: str):
    path = _raw_path(sid)
    return pd.read_pickle(path) if os.path.exists(path) else None


def delete_raw(sid: str) -> None:
    for p in (_raw_path(sid),):
        if os.path.exists(p):
            os.remove(p)


def save_clean_result(sid: str, result) -> None:
    with open(_clean_path(sid), "wb") as f:
        pickle.dump(result, f)


def load_clean_result(sid: str):
    path = _clean_path(sid)
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return pickle.load(f)


def delete_clean(sid: str) -> None:
    p = _clean_path(sid)
    if os.path.exists(p):
        os.remove(p)


# ------------------------------ uploads 檔案紀錄 ---------------------------

def record_upload(sid: str, filename: str, content: bytes) -> tuple[int, str]:
    """把使用者上傳的原始檔位元組存一份到磁碟，並在 uploads 表新增一筆（status=processing）。
    回傳 (upload_id, stored_path)。解析完成後再呼叫 finish_upload() 補上列數與最終狀態。"""
    safe_name = os.path.basename(filename) or "upload"
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    stored_path = os.path.join(_uploads_dir(sid), f"{ts}_{safe_name}")
    with open(stored_path, "wb") as f:
        f.write(content)
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO uploads
                (session_id, filename, stored_path, size_bytes, status, uploaded_at)
            VALUES (?, ?, ?, ?, 'processing', ?)
            """,
            (sid, safe_name, stored_path, len(content), _now()),
        )
        upload_id = cur.lastrowid
    return upload_id, stored_path


def finish_upload(upload_id: int, status: str, row_count=None, error=None) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE uploads SET status = ?, row_count = ?, error = ? WHERE id = ?",
            (status, row_count, error, upload_id),
        )


def list_uploads(sid: str) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id, filename, size_bytes, row_count, status, error, uploaded_at
            FROM uploads WHERE session_id = ? ORDER BY id DESC
            """,
            (sid,),
        ).fetchall()
    return [dict(r) for r in rows]


# ------------------------------ users / auth tokens ------------------------

class EmailTakenError(Exception):
    """註冊時 email 已存在。"""


def create_user(email: str, password_hash: str) -> int:
    with _connect() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
                (email, password_hash, _now()),
            )
        except sqlite3.IntegrityError as e:
            raise EmailTakenError(email) from e
        return cur.lastrowid


def get_user_by_email(email: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE email = ? COLLATE NOCASE", (email,)
        ).fetchone()
    return dict(row) if row else None


def get_user_by_id(user_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def create_token(token: str, user_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO auth_tokens (token, user_id, created_at) VALUES (?, ?, ?)",
            (token, user_id, _now()),
        )


def get_user_id_by_token(token: str) -> int | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT user_id FROM auth_tokens WHERE token = ?", (token,)
        ).fetchone()
    return int(row["user_id"]) if row else None


def delete_token(token: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM auth_tokens WHERE token = ?", (token,))
