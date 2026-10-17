"""认证配置：JWT 密钥、token 有效期、密码哈希上下文。

密钥只从环境变量 LUPIN_JWT_SECRET 读取；未配置时启动生成随机密钥
并打印警告（重启后旧 token 失效，生产环境必须配置）。
"""
import hashlib
import hmac
import os
import secrets
import sys
import warnings

JWT_SECRET = os.environ.get("LUPIN_JWT_SECRET")
if not JWT_SECRET:
    JWT_SECRET = secrets.token_urlsafe(48)
    msg = (
        "[lupin-auth] WARNING: LUPIN_JWT_SECRET 未配置，已生成临时随机密钥；"
        "重启后所有会话失效，生产环境请务必配置该环境变量。"
    )
    print(msg, file=sys.stderr)
    warnings.warn(msg)

JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 15
REFRESH_TOKEN_EXPIRE_DAYS = 7
RESET_TOKEN_EXPIRE_MINUTES = 30
FORGOT_RATE_LIMIT_PER_HOUR = 3
PASSWORD_MIN_LENGTH = 8

# 密码哈希：优先 argon2，其次 bcrypt，最后 pbkdf2_hmac
_HASH_BACKEND = None
try:
    from passlib.context import CryptContext

    for _scheme in ("argon2", "bcrypt"):
        try:
            _ctx = CryptContext(schemes=[_scheme])
            _h = _ctx.hash("lupin-probe")
            if _ctx.verify("lupin-probe", _h):
                _HASH_BACKEND = ("passlib", _scheme)
                _pwd_context = CryptContext(schemes=[_scheme], deprecated="auto")
                break
        except Exception:
            continue
except ImportError:
    pass

if _HASH_BACKEND is None:
    _HASH_BACKEND = ("pbkdf2", None)


def hash_password(password: str) -> str:
    """哈希密码。返回带方案前缀的字符串，绝不存明文/裸 SHA256。"""
    backend, scheme = _HASH_BACKEND
    if backend == "passlib":
        return f"{scheme}${_pwd_context.hash(password)}"
    # 纯标准库兜底：pbkdf2_hmac + 随机盐
    salt = secrets.token_bytes(32)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 600_000)
    return f"pbkdf2${salt.hex()}${dk.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
    """常量时间比较验证密码。"""
    try:
        backend, scheme = _HASH_BACKEND
        if password_hash.startswith("pbkdf2$"):
            _, salt_hex, dk_hex = password_hash.split("$")
            salt = bytes.fromhex(salt_hex)
            dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 600_000)
            return hmac.compare_digest(dk.hex(), dk_hex)
        if "$" in password_hash:
            maybe_scheme, rest = password_hash.split("$", 1)
            if maybe_scheme in ("argon2", "bcrypt"):
                from passlib.context import CryptContext

                ctx = CryptContext(schemes=[maybe_scheme])
                return ctx.verify(password, rest)
        # 兼容无前缀的 passlib 哈希
        from passlib.context import CryptContext

        ctx = CryptContext(schemes=["argon2", "bcrypt"], deprecated="auto")
        return ctx.verify(password, password_hash)
    except Exception:
        return False


def get_hash_backend_name() -> str:
    return _HASH_BACKEND[0] + (f"/{_HASH_BACKEND[1]}" if _HASH_BACKEND[1] else "")
