"""S8.1：媒体存储抽象。

StorageBackend：对象存储统一接口（put/get/delete/exists）。
具体实现：
- local_driver.LocalStorageBackend：本地文件（开发/单机部署）
- s3_driver.S3StorageBackend：S3 兼容（生产，可选依赖 boto3）

get_storage() 按环境变量 LUPIN_STORAGE_BACKEND 选择（local|s3，默认 local）。
"""
from __future__ import annotations

import abc
import os
import re

# 对象 key 白名单：字母数字 + 安全符号，禁止路径穿越
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/]{0,500}$")


def sanitize_key(key: str) -> str:
    """校验对象 key，非法（穿越/绝对路径/非法字符）抛 ValueError。"""
    if not key or not _KEY_RE.match(key):
        raise ValueError(f"非法对象 key: {key!r}")
    parts = key.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"非法对象 key（路径穿越）: {key!r}")
    return key


class StorageBackend(abc.ABC):
    """对象存储抽象。所有实现必须线程安全。"""

    @abc.abstractmethod
    def put(self, key: str, data: bytes) -> None:
        """写入对象（覆盖写）。"""

    @abc.abstractmethod
    def get(self, key: str) -> bytes:
        """读取对象全部字节；不存在抛 FileNotFoundError。"""

    @abc.abstractmethod
    def delete(self, key: str) -> None:
        """删除对象；不存在时静默成功（幂等）。"""

    @abc.abstractmethod
    def exists(self, key: str) -> bool:
        """对象是否存在。"""

    @abc.abstractmethod
    def local_path(self, key: str) -> str | None:
        """本地可直接访问的文件路径；非本地后端返回 None。"""


def get_storage() -> StorageBackend:
    """按环境变量选择存储后端（单例）。"""
    global _singleton
    if _singleton is None:
        backend = os.environ.get("LUPIN_STORAGE_BACKEND", "local").strip().lower()
        if backend == "s3":
            from .s3_driver import S3StorageBackend
            _singleton = S3StorageBackend()
        else:
            from .local_driver import LocalStorageBackend
            _singleton = LocalStorageBackend()
    return _singleton


def reset_storage() -> None:
    """测试用：重置单例（切换 LUPIN_MEDIA_DIR 等配置后调用）。"""
    global _singleton
    _singleton = None


_singleton: StorageBackend | None = None
