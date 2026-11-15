"""Storage abstraction for generated artifacts (renders, thumbnails, audio).

Two backends:
  • LocalStorage  — writes under /app/backend/static/<bucket>/... and returns
                    /api/static/<bucket>/... URLs served by FastAPI StaticFiles.
                    Default. Preserves existing on-disk layout exactly.
  • S3Storage     — uploads to any S3-compatible service (AWS S3, Cloudflare R2,
                    MinIO, Backblaze, Wasabi). Lazy boto3 import. Returns
                    public URL via STORAGE_PUBLIC_BASE_URL when set; otherwise
                    falls back to a presigned URL with TTL.

Public API (used by render/tts/thumbnail_images):
    store = get_storage()
    info = store.save_file(local_path, key, content_type)
    # info = {"url": str, "preview_path": Optional[str], "key": str,
    #         "file_path": Optional[Path], "remote": bool}
    store.download_object(key, dest_path)   # read by storage_key; ignores saved URLs
    resolve_asset_url(asset)                # fresh browser URL from storage_key

Hard rules:
  * The frontend NEVER receives an internal /app/... path. Public payloads must
    use the returned `url`. `file_path` is internal-only and is None after a
    successful object upload.
  * Local mode is byte-for-byte the same as before this module existed.
  * S3 mode is pluggable: only activated when STORAGE_MODE=object.
"""
from __future__ import annotations

import logging
import os
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger("facelessforge.storage")

STATIC_ROOT = Path(__file__).parent.parent / "static"


# Sensitive substrings we never echo back into health responses.
_REDACT_PATTERNS = (
    "AKIA", "ASIA", "aws_secret", "AWS_SECRET", "STORAGE_SECRET",
    "STORAGE_ACCESS", "X-Amz-Signature", "Signature=",
)


def clean_storage_key(key: object) -> Optional[str]:
    """Return a relative object key, or None when it could escape the bucket/root.

    Leading slashes are stripped. Empty segments, ``.`` and ``..`` are rejected
    so a stored key cannot be used as a path traversal.
    """
    if not isinstance(key, str):
        return None
    clean = key.strip().replace("\\", "/").lstrip("/")
    parts = clean.split("/") if clean else []
    if not clean or any(part in ("", ".", "..") for part in parts):
        return None
    return clean


def _safe_error(exc: BaseException, max_len: int = 240) -> str:
    """Stringify an exception without leaking secrets."""
    raw = f"{type(exc).__name__}: {exc}"
    for pat in _REDACT_PATTERNS:
        if pat.lower() in raw.lower():
            return f"{type(exc).__name__}: <redacted>"
    if len(raw) > max_len:
        raw = raw[:max_len] + "…"
    return raw


# ============================ INTERFACE ============================

@dataclass
class SaveResult:
    url: str                       # absolute URL the frontend uses
    preview_path: Optional[str]    # /api/static/... if local; None if remote
    key: str                       # storage key (e.g. "renders/<pid>/<id>.mp4")
    file_path: Optional[Path]      # local on-disk path; None after remote upload
    remote: bool                   # True if served from object storage


class StorageBackend:
    mode: str = "abstract"

    def save_file(self, local_path: Path, key: str, content_type: str) -> SaveResult:
        raise NotImplementedError

    def delete(self, *, key: Optional[str] = None, url: Optional[str] = None) -> bool:
        raise NotImplementedError

    def download_object(self, key: str, dest: Path, *, max_bytes: Optional[int] = None) -> bool:
        """Copy ``key`` onto ``dest``. False when the object cannot be read."""
        return False

    def healthcheck(self) -> dict:
        raise NotImplementedError

    def probe(self) -> dict:
        """Live round-trip probe: upload → verify → delete. Returns:
            {ok: bool, mode: str, latency_ms: int, error: Optional[str], probed_at: iso}
        Never raises. Never returns credentials."""
        raise NotImplementedError


# ============================ LOCAL ============================

class LocalStorage(StorageBackend):
    mode = "local"

    def __init__(self):
        self.root = STATIC_ROOT
        self.root.mkdir(parents=True, exist_ok=True)

    def _abs(self, key: str) -> Path:
        # Sanitise: no leading slash, no backrefs
        clean = key.lstrip("/").replace("\\", "/")
        if ".." in clean.split("/"):
            raise ValueError("invalid key")
        return (self.root / clean).resolve()

    def save_file(self, local_path: Path, key: str, content_type: str) -> SaveResult:
        target = self._abs(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        # If the file is already at the target path (typical: render writes
        # directly to static/renders/...), don't copy.
        if local_path.resolve() != target:
            shutil.copy2(local_path, target)
            try:
                if local_path.exists() and local_path != target:
                    local_path.unlink()
            except Exception:  # noqa: BLE001
                pass
        rel = f"/api/static/{key.lstrip('/')}"
        abs_base = os.environ.get("FRONTEND_URL", "").rstrip("/")
        url = f"{abs_base}{rel}" if abs_base else rel
        return SaveResult(url=url, preview_path=rel, key=key, file_path=target, remote=False)

    def download_object(self, key: str, dest: Path, *, max_bytes: Optional[int] = None) -> bool:
        clean = clean_storage_key(key)
        if not clean:
            return False
        src = self.root / clean
        try:
            # Reject symlink escapes. resolve() follows links, so a link is not
            # equal to the path we were asked to read.
            if src.resolve() != src or not src.is_file():
                return False
            size = src.stat().st_size
        except OSError:
            return False
        if size <= 0 or (max_bytes is not None and size > max_bytes):
            return False
        try:
            if dest.resolve() == src:
                return True
        except OSError:
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        return dest.is_file() and dest.stat().st_size > 0

    def delete(self, *, key: Optional[str] = None, url: Optional[str] = None) -> bool:
        if key:
            try:
                p = self._abs(key)
                if p.exists() and p.is_file():
                    p.unlink()
                    return True
            except Exception:  # noqa: BLE001
                return False
        if url and "/api/static/" in url:
            sub = url.split("/api/static/", 1)[1]
            try:
                p = self._abs(sub)
                if p.exists() and p.is_file():
                    p.unlink()
                    return True
            except Exception:  # noqa: BLE001
                return False
        return False

    def healthcheck(self) -> dict:
        return {
            "mode": "local",
            "ok": self.root.exists(),
            "root": str(self.root),
            "public_url_strategy": "fastapi_static_mount",
            "warning": None,
        }

    def probe(self) -> dict:
        from datetime import datetime as _dt, timezone as _tz
        from secrets import token_hex
        import time
        ts = int(time.time())
        key = f"health/probe-{ts}-{token_hex(4)}.txt"
        path = None
        t0 = time.time()
        try:
            path = self._abs(key)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"facelessfg")  # 10 bytes
            if not path.exists() or path.stat().st_size != 10:
                raise RuntimeError("write verification failed")
            path.unlink()
            return {
                "ok": True, "mode": "local", "error": None,
                "latency_ms": int((time.time() - t0) * 1000),
                "probed_at": _dt.now(_tz.utc).isoformat(),
            }
        except Exception as e:  # noqa: BLE001
            try:
                if path and path.exists():
                    path.unlink()
            except Exception:
                pass
            return {
                "ok": False, "mode": "local",
                "error": _safe_error(e),
                "latency_ms": int((time.time() - t0) * 1000),
                "probed_at": _dt.now(_tz.utc).isoformat(),
            }


# ============================ OBJECT (S3-compatible) ============================

class S3Storage(StorageBackend):
    mode = "object"

    def __init__(self):
        self.bucket = os.environ.get("STORAGE_BUCKET", "")
        self.region = os.environ.get("STORAGE_REGION") or None
        self.endpoint_url = (
            os.environ.get("STORAGE_ENDPOINT_URL")
            or os.environ.get("STORAGE_ENDPOINT")
            or None
        )
        self.public_base = (os.environ.get("STORAGE_PUBLIC_BASE_URL") or "").rstrip("/")
        self.signed_ttl = int(os.environ.get("STORAGE_SIGNED_URL_TTL", "86400"))
        self.access_key = (
            os.environ.get("STORAGE_ACCESS_KEY_ID")
            or os.environ.get("STORAGE_ACCESS_KEY")
            or os.environ.get("AWS_ACCESS_KEY_ID")
        )
        self.secret_key = (
            os.environ.get("STORAGE_SECRET_ACCESS_KEY")
            or os.environ.get("STORAGE_SECRET_KEY")
            or os.environ.get("AWS_SECRET_ACCESS_KEY")
        )
        self._client = None
        self._client_lock = threading.Lock()

    def _client_or_raise(self):
        if self._client is not None:
            return self._client
        with self._client_lock:
            if self._client is not None:
                return self._client
            if not self.bucket:
                raise RuntimeError("STORAGE_BUCKET not set")
            try:
                import boto3
                from botocore.config import Config
            except Exception as e:  # noqa: BLE001
                raise RuntimeError(f"boto3 not installed: {e}")
            kwargs = {"config": Config(signature_version="s3v4", retries={"max_attempts": 3})}
            if self.region:
                kwargs["region_name"] = self.region
            if self.endpoint_url:
                kwargs["endpoint_url"] = self.endpoint_url
            if self.access_key and self.secret_key:
                kwargs["aws_access_key_id"] = self.access_key
                kwargs["aws_secret_access_key"] = self.secret_key
            self._client = boto3.client("s3", **kwargs)
        return self._client

    def _public_or_signed_url(self, key: str) -> str:
        # Fix R2 403: force presigned when STORAGE_FORCE_PRESIGNED=true or R2 bucket detected as private
        force_presigned = (os.environ.get("STORAGE_FORCE_PRESIGNED", "").strip().lower() in ("1","true","yes") or
                           os.environ.get("STORAGE_USE_PRESIGNED","").strip().lower() in ("1","true","yes"))
        # Auto-detect R2 private bucket - videos.ethinx.solutions is not public (403), so use presigned
        is_r2 = bool(self.endpoint_url and "r2.cloudflarestorage.com" in self.endpoint_url)
        if self.public_base and not force_presigned and not is_r2:
            return f"{self.public_base}/{key.lstrip('/')}"
        # Presigned fallback - also for R2 even when public_base set
        try:
            client = self._client_or_raise()
            return client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": key},
                ExpiresIn=self.signed_ttl,
            )
        except Exception as e:
            logger.warning(f"presigned fallback failed for {key}: {e} - returning public_base")
            if self.public_base:
                return f"{self.public_base}/{key.lstrip('/')}"
            raise

    def get_presigned_url(self, key: str, expires_in: Optional[int] = None) -> str:
        client = self._client_or_raise()
        return client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=expires_in or self.signed_ttl,
        )

    def save_file(self, local_path: Path, key: str, content_type: str) -> SaveResult:
        client = self._client_or_raise()
        client.upload_file(
            str(local_path), self.bucket, key,
            ExtraArgs={"ContentType": content_type, "CacheControl": "public, max-age=3600"},
        )
        url = self._public_or_signed_url(key)
        # Remove local file after successful upload (no longer needed by ingress)
        try:
            if local_path.exists() and local_path.is_file():
                local_path.unlink()
        except Exception:  # noqa: BLE001
            pass
        return SaveResult(url=url, preview_path=None, key=key, file_path=None, remote=True)

    def download_object(self, key: str, dest: Path, *, max_bytes: Optional[int] = None) -> bool:
        """Download by key. Does not use a previously signed URL."""
        clean = clean_storage_key(key)
        if not clean:
            return False
        try:
            client = self._client_or_raise()
        except Exception as exc:  # noqa: BLE001
            logger.warning("object download unavailable for %s: %s", clean, _safe_error(exc))
            return False
        if max_bytes is not None:
            try:
                head = client.head_object(Bucket=self.bucket, Key=clean)
            except Exception as exc:  # noqa: BLE001
                logger.warning("object head failed for %s: %s", clean, _safe_error(exc))
                return False
            length = head.get("ContentLength") if hasattr(head, "get") else None
            try:
                if length is not None and int(length) > max_bytes:
                    logger.warning("object %s exceeds %s bytes", clean, max_bytes)
                    return False
            except (TypeError, ValueError):
                return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        partial = dest.parent / (dest.name + ".part")
        try:
            client.download_file(self.bucket, clean, str(partial))
            size = partial.stat().st_size if partial.is_file() else 0
            if size <= 0 or (max_bytes is not None and size > max_bytes):
                partial.unlink(missing_ok=True)
                return False
            partial.replace(dest)
            return dest.is_file() and dest.stat().st_size > 0
        except Exception as exc:  # noqa: BLE001
            logger.warning("object download failed for %s: %s", clean, _safe_error(exc))
            try:
                partial.unlink(missing_ok=True)
            except Exception:
                pass
            return False

    def delete(self, *, key: Optional[str] = None, url: Optional[str] = None) -> bool:
        try:
            client = self._client_or_raise()
        except Exception:
            return False
        target = key
        if not target and url and self.public_base and url.startswith(self.public_base):
            target = url[len(self.public_base):].lstrip("/")
        if not target:
            return False
        try:
            client.delete_object(Bucket=self.bucket, Key=target)
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("S3 delete failed for %s: %s", target, e)
            return False

    def healthcheck(self) -> dict:
        out = {
            "mode": "object",
            "bucket": self.bucket,
            "region": self.region,
            "endpoint_url": self.endpoint_url,
            "public_url_strategy": "public_base_url" if self.public_base else "presigned",
            "public_base_url": self.public_base or None,
            "credentials_present": bool(self.access_key and self.secret_key),
        }
        ok = bool(self.bucket) and bool(self.access_key and self.secret_key)
        out["ok"] = ok
        out["warning"] = None if ok else (
            "Object storage misconfigured: STORAGE_BUCKET plus one of "
            "(STORAGE_ACCESS_KEY_ID/STORAGE_SECRET_ACCESS_KEY), "
            "(STORAGE_ACCESS_KEY/STORAGE_SECRET_KEY), "
            "or (AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY) is required."
        )
        return out

    def probe(self) -> dict:
        """Upload a 10-byte object → head_object → delete. Cheap and short."""
        from datetime import datetime as _dt, timezone as _tz
        from secrets import token_hex
        import time
        ts = int(time.time())
        key = f"health/probe-{ts}-{token_hex(4)}.txt"
        t0 = time.time()
        # Pre-flight: config sanity
        if not self.bucket:
            return {"ok": False, "mode": "object", "error": "STORAGE_BUCKET not set",
                    "latency_ms": 0,
                    "probed_at": _dt.now(_tz.utc).isoformat()}
        try:
            client = self._client_or_raise()
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "mode": "object", "error": _safe_error(e),
                    "latency_ms": int((time.time() - t0) * 1000),
                    "probed_at": _dt.now(_tz.utc).isoformat()}
        # Upload
        try:
            client.put_object(
                Bucket=self.bucket, Key=key, Body=b"facelessfg",
                ContentType="text/plain", CacheControl="no-store",
            )
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "mode": "object",
                    "error": f"upload failed: {_safe_error(e)}",
                    "latency_ms": int((time.time() - t0) * 1000),
                    "probed_at": _dt.now(_tz.utc).isoformat()}
        # Verify
        try:
            client.head_object(Bucket=self.bucket, Key=key)
        except Exception as e:  # noqa: BLE001
            # Try cleanup before returning
            try:
                client.delete_object(Bucket=self.bucket, Key=key)
            except Exception:
                pass
            return {"ok": False, "mode": "object",
                    "error": f"verify failed: {_safe_error(e)}",
                    "latency_ms": int((time.time() - t0) * 1000),
                    "probed_at": _dt.now(_tz.utc).isoformat()}
        # Delete
        try:
            client.delete_object(Bucket=self.bucket, Key=key)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "mode": "object",
                    "error": f"delete failed: {_safe_error(e)}",
                    "latency_ms": int((time.time() - t0) * 1000),
                    "probed_at": _dt.now(_tz.utc).isoformat()}
        return {"ok": True, "mode": "object", "error": None,
                "latency_ms": int((time.time() - t0) * 1000),
                "probed_at": _dt.now(_tz.utc).isoformat()}


# ============================ ACCESSOR ============================

_BACKEND: Optional[StorageBackend] = None
_BACKEND_LOCK = threading.Lock()


def _build_backend() -> StorageBackend:
    mode = (os.environ.get("STORAGE_MODE", "local") or "local").strip().lower()
    if mode == "object":
        return S3Storage()
    return LocalStorage()


def get_storage() -> StorageBackend:
    global _BACKEND
    if _BACKEND is not None:
        return _BACKEND
    with _BACKEND_LOCK:
        if _BACKEND is None:
            _BACKEND = _build_backend()
    return _BACKEND


def resolve_asset_url(asset: Optional[dict]) -> Optional[str]:
    """Return a browser URL that works right now for a stored asset.

    Object-storage preview URLs are presigned when the file is saved and expire
    after STORAGE_SIGNED_URL_TTL (default 86400 seconds). A record that still
    has ``storage_key`` is signed again here. Local files keep their stable
    ``/api/static`` path. Stock and other external assets have no storage key,
    so the stored URL is returned unchanged. This does not modify ``asset``.
    """
    if not asset:
        return None
    backend = get_storage()
    key = clean_storage_key(asset.get("storage_key"))
    if key and getattr(backend, "mode", None) == "object" and hasattr(backend, "get_presigned_url"):
        try:
            return backend.get_presigned_url(key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("asset url refresh failed for %s: %s", key, _safe_error(exc))
    if getattr(backend, "mode", None) == "local":
        for field in ("preview_path", "preview_url", "url"):
            value = asset.get(field)
            if isinstance(value, str) and value.startswith("/api/static/"):
                return value
    for field in ("preview_url", "url", "download_url"):
        value = asset.get(field)
        if isinstance(value, str) and value:
            return value
    return None


def resolve_delivery_url(job: Optional[dict]) -> Optional[str]:
    """Return a currently usable URL for a persisted render job.

    Object-storage URLs are signed when requested rather than trusting the
    time-limited URL saved when the render originally completed. Local storage
    keeps using its stable FastAPI preview path.
    """
    if not job:
        return None
    backend = get_storage()
    key = job.get("output_storage_key")
    if key and backend.mode == "object" and hasattr(backend, "get_presigned_url"):
        return backend.get_presigned_url(key)
    if backend.mode == "local" and job.get("output_relative_url"):
        return job["output_relative_url"]
    return job.get("output_url")


def reset_for_tests() -> None:
    """Drop the cached singleton — only used by tests that flip STORAGE_MODE."""
    global _BACKEND
    with _BACKEND_LOCK:
        _BACKEND = None


def storage_status() -> dict:
    """Returns a dict suitable for /api/admin/diagnostics."""
    backend = get_storage()
    info = backend.healthcheck()
    info["retention_days"] = int(os.environ.get("STORAGE_RETENTION_DAYS",
                                                os.environ.get("RENDER_RETENTION_DAYS", "7")))
    dev_mode = os.environ.get("DEV_MODE", "false").lower() in ("1", "true", "yes")
    if not dev_mode and info["mode"] == "local":
        info["warning"] = ("STORAGE_MODE=local in production — generated MP4s, "
                           "thumbnails, and audio do NOT survive a pod rebuild. "
                           "Set STORAGE_MODE=object and configure a bucket.")
    return info


def make_url(local_relative_path: str) -> Tuple[str, Optional[str]]:
    """Helper for callers that already wrote a file under static/...
    Returns (absolute_url, preview_path). Local mode returns the existing
    /api/static/... URL pair; object mode is unsupported here (callers must
    use save_file)."""
    rel = local_relative_path if local_relative_path.startswith("/") else f"/{local_relative_path}"
    abs_base = os.environ.get("FRONTEND_URL", "").rstrip("/")
    return (f"{abs_base}{rel}" if abs_base else rel, rel)
