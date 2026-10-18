"""S8.1：S3 兼容存储驱动（生产预留）。

boto3 为可选依赖：未安装时实例化抛错并给出安装指引，不影响
local 驱动与现有功能。

环境变量：
- LUPIN_S3_BUCKET（必填）
- LUPIN_S3_ENDPOINT（可选，兼容 MinIO / R2 等）
- LUPIN_S3_REGION（默认 auto）
- LUPIN_S3_ACCESS_KEY / LUPIN_S3_SECRET_KEY（建议走 IAM，此处仅兜底）
"""
from __future__ import annotations

import os

from .storage import StorageBackend, sanitize_key


class S3StorageBackend(StorageBackend):
    def __init__(self) -> None:
        try:
            import boto3  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "S3 存储后端需要 boto3：pip install boto3"
            ) from e
        self.bucket = os.environ.get("LUPIN_S3_BUCKET", "").strip()
        if not self.bucket:
            raise RuntimeError("LUPIN_S3_BUCKET 未配置")
        endpoint = os.environ.get("LUPIN_S3_ENDPOINT", "").strip() or None
        self._s3 = boto3.client(
            "s3",
            endpoint_url=endpoint,
            region_name=os.environ.get("LUPIN_S3_REGION", "auto"),
            aws_access_key_id=os.environ.get("LUPIN_S3_ACCESS_KEY") or None,
            aws_secret_access_key=os.environ.get("LUPIN_S3_SECRET_KEY") or None,
        )

    def put(self, key: str, data: bytes) -> None:
        sanitize_key(key)
        self._s3.put_object(Bucket=self.bucket, Key=key, Body=data)

    def get(self, key: str) -> bytes:
        sanitize_key(key)
        try:
            resp = self._s3.get_object(Bucket=self.bucket, Key=key)
            return resp["Body"].read()
        except self._s3.exceptions.NoSuchKey as e:
            raise FileNotFoundError(f"对象不存在: {key!r}") from e

    def delete(self, key: str) -> None:
        sanitize_key(key)
        self._s3.delete_object(Bucket=self.bucket, Key=key)  # S3 删除幂等

    def exists(self, key: str) -> bool:
        sanitize_key(key)
        try:
            self._s3.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:  # noqa: BLE001 — 404 等一律视为不存在
            return False

    def local_path(self, key: str) -> str | None:
        return None  # S3 无本地路径，下载走预签名 URL

    def presigned_url(self, key: str, expires_in: int = 300) -> str:
        """S3 预签名下载 URL（生产推荐方式）。"""
        sanitize_key(key)
        return self._s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=expires_in,
        )
