"""S8.1：媒体存储包。

- storage：StorageBackend 抽象 + get_storage() 工厂
- local_driver：本地文件实现（默认）
- s3_driver：S3 兼容实现（可选依赖 boto3，生产预留）
- validation：上传校验（魔数 + 大小限制）
- signing：短期签名下载 URL（HMAC）
- probe：元数据提取（图片头解析 / ffprobe）
- worker：媒体处理后台线程
"""
from .storage import StorageBackend, get_storage, reset_storage, sanitize_key

__all__ = ["StorageBackend", "get_storage", "reset_storage", "sanitize_key"]
