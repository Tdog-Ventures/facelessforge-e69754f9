"""Stock photos are footage. A JPEG must not die in the video-duration probe."""
import asyncio
import subprocess
from pathlib import Path

from app.asset_relevance import _inspect_one, probe_still_image


def _jpeg(dest: Path) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error", "-f", "lavfi",
            "-i", "color=c=green:s=96x64", "-frames:v", "1", str(dest),
        ],
        check=True,
    )


def _scene():
    return {
        "narration_text": "Pickers harvest red coffee cherries on the farm.",
        "visual_direction": "coffee cherries on a branch",
        "search_terms": ["coffee cherries branch"],
    }


def _photo():
    return {
        "external_id": "photo-1",
        "download_url": "https://images.example/coffee.jpg",
        "preview_url": "https://images.example/coffee.jpg",
        "source": "pexels",
        "source_url": "https://www.pexels.com/photo/coffee-cherries-on-a-branch-1/",
        "title": "Red coffee cherries on a branch",
        "media_type": "stock_image",
    }


def test_jpeg_is_judged_instead_of_rejected_as_a_video(tmp_path, monkeypatch):
    sample = tmp_path / "sample.jpg"
    _jpeg(sample)
    jpeg = sample.read_bytes()

    async def download(_url, dest):
        Path(dest).write_bytes(jpeg)
        return True

    async def judge(_image, _mime, _narration, _query):
        return {"accepted": True, "score": 90, "reason": "coffee cherries on a branch"}

    monkeypatch.setattr("app.asset_relevance.download_candidate", download)
    monkeypatch.setattr("app.asset_relevance.judge_frame", judge)
    result = asyncio.run(_inspect_one(_photo(), _scene(), "coffee cherries branch"))
    assert result.get("reject_reason") is None
    assert result["visual_relevance"]["accepted"] is True
    assert result["media_type"] == "stock_image"
    assert result["width"] >= 64 and result["height"] >= 64


def test_plain_text_is_still_probe_failed(tmp_path, monkeypatch):
    async def download(_url, dest):
        Path(dest).write_bytes(b"this is not a picture or a video. " * 400)
        return True

    async def judge(*_args):
        raise AssertionError("a non-image must not be sent to the frame judge")

    monkeypatch.setattr("app.asset_relevance.download_candidate", download)
    monkeypatch.setattr("app.asset_relevance.judge_frame", judge)
    result = asyncio.run(_inspect_one(_photo(), _scene(), "coffee cherries branch"))
    assert result["reject_reason"] == "probe_failed"


def test_probe_still_image_rejects_text(tmp_path):
    path = tmp_path / "note.txt"
    path.write_bytes(b"hello " * 100)
    assert asyncio.run(probe_still_image(path)) is None
