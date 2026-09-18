"""Boot-time system checks for FacelessForge.

Ensures `ffmpeg` and `ffprobe` are available so the render queue works
on a freshly-rebuilt container. Falls back to the static `imageio-ffmpeg`
binary for ffmpeg if apt install fails.

Runs once during FastAPI lifespan startup. Logs but never blocks startup.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess

logger = logging.getLogger("facelessforge.system")


def _which(name: str) -> str | None:
    return shutil.which(name)


def _try_apt_install_ffmpeg() -> bool:
    """Idempotent — returns True if ffmpeg + ffprobe are now resolvable."""
    if _which("ffmpeg") and _which("ffprobe"):
        return True
    if os.environ.get("DISABLE_APT_BOOT_INSTALL", "").lower() in ("1", "true", "yes"):
        return False
    if not shutil.which("apt-get"):
        return False
    try:
        env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
        # Try install only — assume index is sufficiently fresh; apt-get update
        # is too slow / network-flaky on cold pods.
        subprocess.run(
            ["apt-get", "install", "-y", "--no-install-recommends", "ffmpeg"],
            env=env, check=False, timeout=180,
            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("apt-get install ffmpeg failed: %s", e)
        return False
    return bool(_which("ffmpeg") and _which("ffprobe"))


def probe_ffmpeg_status() -> dict:
    """Lightweight, side-effect-free ffmpeg/ffprobe probe (no apt install)."""
    sys_ffmpeg = _which("ffmpeg")
    sys_ffprobe = _which("ffprobe")
    return {
        "ffmpeg": sys_ffmpeg,
        "ffprobe": sys_ffprobe,
        "ffmpeg_source": "apt" if sys_ffmpeg else None,
        "ffprobe_source": "apt" if sys_ffprobe else None,
    }


def ensure_ffmpeg_available() -> dict:
    """Make sure ffmpeg/ffprobe are present. Returns a small status dict."""
    apt_ok = _try_apt_install_ffmpeg()
    sys_ffmpeg = _which("ffmpeg")
    sys_ffprobe = _which("ffprobe")

    pip_fallback = None
    if not sys_ffmpeg:
        try:
            import imageio_ffmpeg
            pip_fallback = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:  # noqa: BLE001
            pip_fallback = None

    status = {
        "ffmpeg": sys_ffmpeg or pip_fallback,
        "ffprobe": sys_ffprobe,
        "ffmpeg_source": "apt" if sys_ffmpeg else ("imageio-ffmpeg" if pip_fallback else None),
        "ffprobe_source": "apt" if sys_ffprobe else None,
        "apt_install_attempted": True,
        "apt_install_ok": apt_ok,
    }
    if status["ffmpeg"]:
        logger.info("ffmpeg=%s (%s)", status["ffmpeg"], status["ffmpeg_source"])
    else:
        logger.error("ffmpeg unavailable — render queue will fail")
    if status["ffprobe"]:
        logger.info("ffprobe=%s", status["ffprobe"])
    else:
        logger.warning("ffprobe unavailable — render duration will be estimated")
    return status
