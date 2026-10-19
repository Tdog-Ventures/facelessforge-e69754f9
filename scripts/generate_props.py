#!/usr/bin/env python3
"""
generate_props.py - Bridge Python -> Remotion
Converts FacelessForge project.json into remotion-renderer/public/props.json
and copies assets to public/ so staticFile() works.

Usage:
  python generate_props.py --project /root/facelessforge-deploy/projects/<id>.json
  python generate_props.py --project ./project.json --output ./remotion-renderer/public/props.json

It enforces Constitution rules:
- landscape only, width >=1280
- scene duration = ffprobe(audio) not fixed slot
- captions = 6-8 words, timed proportionally
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
import re

def ffprobe_duration(path: Path) -> float | None:
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path)
        ]
        out = subprocess.check_output(cmd, text=True).strip()
        return float(out)
    except Exception:
        # try stream duration
        try:
            cmd = [
                "ffprobe", "-v", "error",
                "-select_streams", "a:0",
                "-show_entries", "stream=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path)
            ]
            out = subprocess.check_output(cmd, text=True).strip().splitlines()[0]
            return float(out)
        except Exception as e:
            print(f"[warn] ffprobe failed for {path}: {e}", file=sys.stderr)
            return None

def ffprobe_dims(path: Path) -> tuple[int,int] | None:
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "json",
            str(path)
        ]
        out = subprocess.check_output(cmd, text=True)
        data = json.loads(out)
        s = data["streams"][0]
        return int(s["width"]), int(s["height"])
    except Exception:
        return None

def split_captions(text: str, total_duration: float, min_words=6, max_words=8):
    if not text:
        return []
    words = re.findall(r"\S+", text.strip())
    chunks = []
    i = 0
    while i < len(words):
        # take 6-8 words, try not to leave 1-2 words orphan
        remaining = len(words) - i
        if remaining <= max_words + 2 and remaining > max_words:
            take = remaining // 2
        else:
            take = min(max_words, remaining)
            if remaining > max_words and remaining - take < min_words:
                take = remaining - min_words
        chunk_words = words[i:i+take]
        chunks.append(" ".join(chunk_words))
        i += take

    # time proportionally
    total_chars = sum(len(c) for c in chunks) or 1
    captions = []
    acc = 0.0
    for idx, chunk in enumerate(chunks):
        # weight by char length for more natural timing
        weight = len(chunk) / total_chars
        dur = total_duration * weight
        start = acc
        end = acc + dur if idx < len(chunks)-1 else total_duration
        captions.append({"text": chunk, "start": round(start,3), "end": round(end,3)})
        acc = end
    return captions


def _call_whisper_api(audio_path: Path, api_key: str) -> list[dict]:
    """Call OpenAI Whisper verbose_json word-level transcription. Falls back to []."""
    url = "https://api.openai.com/v1/audio/transcriptions"
    cmd = [
        "curl", "-s", "--fail", url,
        "-H", f"Authorization: Bearer {api_key}",
        "-F", f"file=@{audio_path}",
        "-F", "model=whisper-1",
        "-F", "response_format=verbose_json",
        "-F", "timestamp_granularities[]=word",
    ]
    try:
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)
        data = json.loads(out)
        words = data.get("words") or []
        if not words:
            print("[whisper] no word-level timestamps returned", file=sys.stderr)
            return []
        captions = []
        i = 0
        while i < len(words):
            chunk_words = words[i:i + 7]
            text = " ".join(w.get("word", "") for w in chunk_words).strip()
            if text:
                captions.append({
                    "text": text,
                    "start": float(chunk_words[0].get("start", 0)),
                    "end": float(chunk_words[-1].get("end", chunk_words[-1].get("start", 0))),
                })
            i += 7
        print(f"[whisper] generated {len(captions)} captions from {len(words)} words")
        return captions
    except Exception as e:
        print(f"[whisper] transcription failed: {e}", file=sys.stderr)
        return []


def maybe_whisper_captions(audio_path: Path, fallback_text: str, duration: float) -> list[dict]:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if api_key and audio_path.suffix.lower() in (".mp3", ".mp4", ".mpeg", ".mpga", ".m4a", ".wav", ".webm"):
        whisper_caps = _call_whisper_api(audio_path, api_key)
        if whisper_caps:
            # ensure last caption ends exactly at audio duration
            if whisper_caps:
                whisper_caps[-1]["end"] = max(whisper_caps[-1]["end"], duration)
            return whisper_caps
    return split_captions(fallback_text, duration)


def find_project_file(arg: Path) -> Path:
    if arg.is_file():
        return arg
    if arg.is_dir():
        # try common names
        for name in ["project.json", "project_b77ab802.json", "input.json"]:
            p = arg / name
            if p.exists():
                return p
        # newest json in dir
        jsons = sorted(arg.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True)
        if jsons:
            return jsons[0]
    raise FileNotFoundError(f"No project.json found at {arg}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True, help="Path to project.json or its directory")
    ap.add_argument("--output", default="/root/facelessforge-deploy/remotion-renderer/public/props.json")
    ap.add_argument("--public-dir", default="/root/facelessforge-deploy/remotion-renderer/public")
    args = ap.parse_args()

    proj_path = find_project_file(Path(args.project))
    public_dir = Path(args.public_dir)
    output_path = Path(args.output)

    print(f"[info] Loading {proj_path}")
    data = json.loads(proj_path.read_text())

    # normalize scenes structure - support multiple legacy formats
    raw_scenes = data.get("scenes") or data.get("script") or data.get("segments") or []
    if isinstance(raw_scenes, dict):
        raw_scenes = list(raw_scenes.values())

    # prepare public subfolders
    (public_dir / "audio").mkdir(parents=True, exist_ok=True)
    (public_dir / "clips").mkdir(parents=True, exist_ok=True)
    (public_dir / "music").mkdir(parents=True, exist_ok=True)

    out_scenes = []
    base_dir = proj_path.parent

    for idx, sc in enumerate(raw_scenes):
        # extract fields with fallbacks
        narration_text = sc.get("narration") or sc.get("text") or sc.get("voiceover") or sc.get("narration_text") or ""
        audio_src = sc.get("audio") or sc.get("narration_audio") or sc.get("audio_path") or sc.get("voice_path") or ""
        clips = sc.get("clips") or sc.get("videos") or sc.get("stock") or sc.get("footage") or []
        if isinstance(clips, str):
            clips = [clips]

        # resolve audio path
        audio_path = Path(audio_src) if audio_src else None
        if audio_path and not audio_path.is_absolute():
            # try relative to project file, then to deploy root
            candidates = [base_dir / audio_path, Path("/root/facelessforge-deploy") / audio_path, Path(audio_path)]
            audio_path = next((p for p in candidates if p.exists()), candidates[0])

        if not audio_path or not audio_path.exists():
            print(f"[skip] scene {idx}: audio not found {audio_src}", file=sys.stderr)
            continue

        duration = ffprobe_duration(audio_path)
        if not duration or duration < 0.5:
            print(f"[skip] scene {idx}: invalid duration {duration} for {audio_path}", file=sys.stderr)
            continue

        # filter and copy clips
        valid_clips = []
        for c in clips[:12]:  # limit to 12 candidates to avoid huge copy
            c_path = Path(c)
            if not c_path.is_absolute():
                cands = [base_dir / c_path, Path("/root/facelessforge-deploy") / c_path, Path(c_path)]
                c_path = next((p for p in cands if p.exists()), None)
                if not c_path:
                    continue
            dims = ffprobe_dims(c_path)
            if not dims:
                continue
            w, h = dims
            if h >= w:
                print(f"[filter] portrait {c_path} {w}x{h} rejected")
                continue
            if w < 1280:
                print(f"[filter] too small {c_path} {w}x{h} rejected")
                continue
            # copy
            dest = public_dir / "clips" / f"scene{idx}_{c_path.name}"
            if not dest.exists() or c_path.stat().st_mtime > dest.stat().st_mtime:
                shutil.copy2(c_path, dest)
            valid_clips.append(f"clips/{dest.name}")

        if not valid_clips:
            print(f"[warn] scene {idx}: no valid landscape clips, will use placeholder color but keep duration")

        # copy audio
        audio_dest = public_dir / "audio" / f"scene{idx}_{audio_path.name}"
        if not audio_dest.exists() or audio_path.stat().st_mtime > audio_dest.stat().st_mtime:
            shutil.copy2(audio_path, audio_dest)

        captions = maybe_whisper_captions(audio_path, narration_text, duration)

        out_scenes.append({
            "narrationSrc": f"audio/{audio_dest.name}",
            "narrationDuration": round(duration, 3),
            "clips": valid_clips,
            "captions": captions,
            "originalText": narration_text[:200]
        })

    # music
    music_out = None
    music_src = data.get("music") or data.get("background_music")
    if music_src:
        m_path = Path(music_src)
        if not m_path.is_absolute():
            m_path = base_dir / m_path
        if m_path.exists():
            m_dest = public_dir / "music" / m_path.name
            shutil.copy2(m_path, m_dest)
            music_out = f"music/{m_dest.name}"

    props = {
        "scenes": out_scenes,
        "music": music_out,
        "meta": {
            "source_project": str(proj_path),
            "total_narration": round(sum(s["narrationDuration"] for s in out_scenes),3),
            "total_with_intro": round(sum(s["narrationDuration"] for s in out_scenes) + 2.5,3),
            "generated_by": "generate_props.py constitution v1"
        }
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(props, indent=2))
    print(f"\n[done] Wrote {output_path}")
    print(f"  scenes: {len(out_scenes)}")
    print(f"  total narration: {props['meta']['total_narration']}s")
    print(f"  total with intro: {props['meta']['total_with_intro']}s")
    print(f"\nNext:")
    print(f"  cd /root/facelessforge-deploy/remotion-renderer")
    print(f"  npx remotion render FacelessForgeVideo out/real.mp4 --props={output_path}")
    print(f"  # thumbnails (fixes your blocker):")
    print(f"  npx remotion still FacelessForgeVideo out/thumb0.png --frame=5 --props={output_path}")
    print(f"  npx remotion still FacelessForgeVideo out/thumb1.png --frame={int(props['meta']['total_with_intro']*30/3)} --props={output_path}")
    print(f"  npx remotion still FacelessForgeVideo out/thumb2.png --frame={int(props['meta']['total_with_intro']*30*2/3)} --props={output_path}")

if __name__ == "__main__":
    main()
