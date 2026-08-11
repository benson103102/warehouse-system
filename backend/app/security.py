"""
密碼雜湊與登入 token（只用 Python 標準函式庫，不引入 passlib／bcrypt 等額外套件）。

密碼永遠不明文存放：用 PBKDF2-HMAC-SHA256 + 每位使用者獨立的隨機 salt 雜湊後才存進
資料庫。驗證時用 hmac.compare_digest 做定值時間比較，避免時間側通道。

儲存格式（單一字串，方便存一個欄位）：
    pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>

token 用 secrets.token_urlsafe 產生的高熵隨機字串，對應關係存在 auth_tokens 表
（不可逆、可即時撤銷）。刻意不用 JWT：省去簽章金鑰管理，登出＝刪一列即可。
"""

import hashlib
import hmac
import os
import secrets

_ITERATIONS = 200_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return f"pbkdf2_sha256${_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters)
        )
        return hmac.compare_digest(digest.hex(), hash_hex)
    except (ValueError, AttributeError):
        return False


def new_token() -> str:
    return secrets.token_urlsafe(32)
