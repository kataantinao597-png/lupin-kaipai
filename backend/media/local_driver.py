"""S8.1：本地文件存储驱动。

文件落在 <LUPIN_MEDIA_DIR|backend/media_files>/{key}，key 形如
`{org_id}/{work_id}/{uuid}_{safe_filename}`。

- put：先写临时文件再 os.replace，原子替换，不留半截文件
- get：读全部字节；local_path 供 FileResponse 零拷贝发送
- delete：幂等
"""
from __future__ import annotations

import os
import tempfile

from .storage import StorageBackend, sanitize_key

DEFAULT_DIR_NAME = "media_files"


def _default_root() -> str:
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.environ.get("LUPIN_MEDIA_DIR", os.path.join(base, DEFAULT_DIR_NAME))


class LocalStorageBackend(StorageBackend):
    def __init__(self, root: str | None = None):
        self.root = os.path.abspath(root or _default_root())
        os.makedirs(self.root, exist_ok=True)

    def _path(self, key: str) -> str:
        sanitize_key(key)
        # normpath + 前缀检查，防穿越
        p = os.path.normpath(os.path.join(self.root, key))
        if not p.startswith(self.root + os.sep):
            raise ValueError(f"非法对象 key（路径穿越）: {key!r}")
        return p

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # 原子写：同目录临时文件 + replace
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path),
                                   prefix=".upload-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def get(self, key: str) -> bytes:
        path = self._path(key)
        try:
            with open(path, "rb") as f:
                return f.read()
        except FileNotFoundError:
            raise FileNotFoundError(f"对象不存在: {key!r}")

    def delete(self, key: str) -> None:
        path = self._path(key)
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass  # 幂等
        # 顺手清理空父目录（最多两层：work_id/org_id），失败忽略
        for _ in range(2):
            parent = os.path.dirname(path)
            if parent == self.root:
                break
            try:
                os.rmdir(parent)
            except OSError:
                break
            path = parent

    def exists(self, key: str) -> bool:
        return os.path.isfile(self._path(key))

    def local_path(self, key: str) -> str | None:
        path = self._path(key)
        return path if os.path.isfile(path) else None
