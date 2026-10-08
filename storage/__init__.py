"""
Object storage for job files (uploads, cleaned output).

Keys look like  "<job_id>/source.csv"  and  "<job_id>/cleaned.csv".

  LocalStorage - keys map onto TEMP_DIR/<job_id>/<name>, exactly the layout the app has always
                 used. Single instance / dev only: not shared between machines.
  S3Storage    - any S3-compatible service. PRIVATE bucket, server-side encryption requested,
                 downloads via short-lived presigned URLs. Credentials stay on the server.

Pick with OMIXA_STORAGE_BACKEND=local|s3. Expiry in S3 mode is enforced twice: by the worker
reaper (which deletes everything for jobs older than JOB_TTL_SECONDS) and, as a backstop, by a
bucket lifecycle rule you must configure (see docs/SCALING.md).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from typing import BinaryIO, Optional

from config import Config

logger = logging.getLogger("omixa.storage")

_KEY_RE = re.compile(r"^[0-9a-fA-F\-]{36}/(source|cleaned)\.(csv|xlsx|xls)$")


class StorageError(RuntimeError):
    pass


def check_key(key: str) -> str:
    """Keys are only ever built by us from a validated uuid + fixed names; refuse anything else
    so a bug can never turn into path traversal or an arbitrary-object read."""
    if not _KEY_RE.match(key or ""):
        raise StorageError("invalid storage key")
    return key


def source_key(job_id: str, ext: str) -> str:
    return check_key(f"{job_id}/source.{ext}")


def cleaned_key(job_id: str, ext: str) -> str:
    return check_key(f"{job_id}/cleaned.{ext}")


class LocalStorage:
    name = "local"

    def _path(self, key: str) -> str:
        return os.path.join(Config.TEMP_DIR, *check_key(key).split("/"))

    def put_fileobj(self, key: str, fileobj: BinaryIO, content_type: str = "application/octet-stream") -> int:
        path = self._path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as out:
            shutil.copyfileobj(fileobj, out, 1024 * 1024)
        return os.path.getsize(path)

    def put_path(self, key: str, src: str, content_type: str = "application/octet-stream") -> int:
        dest = self._path(key)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if os.path.abspath(src) != os.path.abspath(dest):
            shutil.copyfile(src, dest)
        return os.path.getsize(dest)

    def get_to_path(self, key: str, dest: str) -> None:
        src = self._path(key)
        if not os.path.isfile(src):
            raise StorageError("object not found")
        if os.path.abspath(src) != os.path.abspath(dest):
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copyfile(src, dest)

    def exists(self, key: str) -> bool:
        return os.path.isfile(self._path(key))

    def delete(self, key: str) -> None:
        try:
            os.remove(self._path(key))
        except FileNotFoundError:
            pass

    def delete_job(self, job_id: str) -> None:
        for ext in ("csv", "xlsx", "xls"):
            for kind in ("source", "cleaned"):
                self.delete(f"{job_id}/{kind}.{ext}")

    def presigned_get(self, key: str, filename: str, ttl: int | None = None) -> Optional[str]:
        return None  # local files are streamed by the API itself


class S3Storage:
    name = "s3"

    def __init__(self, client=None, bucket: str | None = None):
        self.bucket = bucket or Config.S3_BUCKET
        if not self.bucket:
            raise StorageError("OMIXA_S3_BUCKET is not set")
        self._client = client

    @property
    def client(self):
        if self._client is None:
            import boto3  # lazy
            from botocore.config import Config as BotoConfig
            kwargs = {
                "region_name": Config.S3_REGION or None,
                "config": BotoConfig(signature_version="s3v4", retries={"max_attempts": 4, "mode": "standard"},
                                     connect_timeout=5, read_timeout=60),
            }
            if Config.S3_ENDPOINT_URL:
                kwargs["endpoint_url"] = Config.S3_ENDPOINT_URL
            if Config.S3_ACCESS_KEY_ID and Config.S3_SECRET_ACCESS_KEY:
                kwargs["aws_access_key_id"] = Config.S3_ACCESS_KEY_ID
                kwargs["aws_secret_access_key"] = Config.S3_SECRET_ACCESS_KEY
            self._client = boto3.client("s3", **kwargs)
        return self._client

    def _k(self, key: str) -> str:
        prefix = Config.S3_PREFIX
        return f"{prefix}/{check_key(key)}" if prefix else check_key(key)

    def _extra(self) -> dict:
        return {"ServerSideEncryption": Config.S3_SSE} if Config.S3_SSE else {}

    def put_fileobj(self, key: str, fileobj: BinaryIO, content_type: str = "application/octet-stream") -> int:
        try:
            self.client.upload_fileobj(fileobj, self.bucket, self._k(key),
                                       ExtraArgs={"ContentType": content_type, **self._extra()})
            return int(self.client.head_object(Bucket=self.bucket, Key=self._k(key))["ContentLength"])
        except Exception as exc:
            raise StorageError(f"upload failed ({type(exc).__name__})") from exc

    def put_path(self, key: str, src: str, content_type: str = "application/octet-stream") -> int:
        try:
            self.client.upload_file(src, self.bucket, self._k(key),
                                    ExtraArgs={"ContentType": content_type, **self._extra()})
            return os.path.getsize(src)
        except Exception as exc:
            raise StorageError(f"upload failed ({type(exc).__name__})") from exc

    def get_to_path(self, key: str, dest: str) -> None:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        try:
            self.client.download_file(self.bucket, self._k(key), dest)
        except Exception as exc:
            raise StorageError(f"download failed ({type(exc).__name__})") from exc

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=self._k(key))
            return True
        except Exception:
            return False

    def delete(self, key: str) -> None:
        try:
            self.client.delete_object(Bucket=self.bucket, Key=self._k(key))
        except Exception:
            logger.warning("storage delete failed (will be retried by the reaper / lifecycle rule)")

    def delete_job(self, job_id: str) -> None:
        for ext in ("csv", "xlsx", "xls"):
            for kind in ("source", "cleaned"):
                self.delete(f"{job_id}/{kind}.{ext}")

    def presigned_get(self, key: str, filename: str, ttl: int | None = None) -> Optional[str]:
        safe = re.sub(r'[^A-Za-z0-9._ -]', "_", filename or "cleaned")[:120]
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": self._k(key),
                    "ResponseContentDisposition": f'attachment; filename="{safe}"'},
            ExpiresIn=int(ttl or Config.SIGNED_URL_TTL_SECONDS),
        )


_storage = None


def get_storage():
    global _storage
    if _storage is None:
        _storage = S3Storage() if Config.STORAGE_BACKEND == "s3" else LocalStorage()
    return _storage


def set_storage_for_tests(obj) -> None:
    global _storage
    _storage = obj


def is_remote() -> bool:
    """True when job files are not on this machine's disk (so API and worker can be on
    different hosts)."""
    return get_storage().name != "local"
