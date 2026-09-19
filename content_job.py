#!/usr/bin/env python3
"""Generate a local, reviewable social-copy draft through a locally hosted model endpoint."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from server import ROOT, load_config
from sovereign_db import SovereignStore

DATA_DIR = Path(os.environ.get("ETHINX_DATA_DIR", ROOT / "data"))

ROTATION: dict[int, dict[str, str]] = {
    0: {"type": "authority", "focus": "How a solo founder broke through the vibe-coding wall without a team or venture funding.", "cta": "See the engine at {website_url}."},
    1: {"type": "proof", "focus": "Describe a concrete, supplied engineering proof point and why it matters to a buyer.", "cta": "See the engine at {website_url}."},
    2: {"type": "offer", "focus": "Explain the Individual and Agency offers without pressure or unverified scarcity.", "cta": "Apply at {website_url}."},
    3: {"type": "comparison", "focus": "Contrast real production pipeline execution with a generic demo, without naming competitors.", "cta": "See the engine at {website_url}."},
    4: {"type": "story", "focus": "Tell the grounded founder story and connect it to the operating principles of the product.", "cta": "See the engine at {website_url}."},
}


def build_prompt(content_config: dict[str, Any], rotation: dict[str, str]) -> str:
    proof_points = "\n".join(f"- {point}" for point in content_config.get("proof_points", []))
    return f"""You are the local ETHINX editorial engine. Produce a factual social media draft for {content_config['product_name']}.

This is a draft for human review. Do not claim a fact that is not in the supplied context. Do not invent customer results, urgency, availability, certifications, metrics, or external integrations. Do not say 'game-changing', 'innovative', 'revolutionary', 'excited to announce', or 'thrilled'. Lead with a concrete fact, not a question.

Founder context:
{content_config['founder_context']}

Supplied product proof points:
{proof_points}

Today’s angle:
- Type: {rotation['type']}
- Focus: {rotation['focus']}
- CTA: {rotation['cta'].format(website_url=content_config['website_url'])}

Return exactly one JSON object with no markdown fencing:
{{"linkedin":"A post at most 1300 characters, plain text with optional line breaks and hashtags only at the end.","x":"A single post at most 240 characters, ending with the configured website URL."}}"""


def generate(ollama_url: str, model: str, prompt: str, timeout_seconds: int = 120) -> dict[str, str]:
    body = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.35},
    }).encode("utf-8")
    request = Request(ollama_url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Local model request failed: {exc}") from exc
    raw = payload.get("response")
    if not isinstance(raw, str):
        raise RuntimeError("Local model response did not contain a string response field.")
    try:
        draft = json.loads(raw.strip())
    except json.JSONDecodeError as exc:
        raise RuntimeError("Local model output was not valid JSON.") from exc
    linkedin = draft.get("linkedin")
    x_post = draft.get("x")
    if not isinstance(linkedin, str) or not isinstance(x_post, str):
        raise RuntimeError("Local model output must contain string linkedin and x fields.")
    linkedin, x_post = linkedin.strip(), x_post.strip()
    if not linkedin or not x_post:
        raise RuntimeError("Local model output may not contain empty posts.")
    if len(linkedin) > 1300 or len(x_post) > 240:
        raise RuntimeError("Local model output exceeded a platform character limit.")
    return {"linkedin": linkedin, "x": x_post}


def generate_template(content_config: dict[str, Any], rotation: dict[str, str]) -> dict[str, str]:
    """Produce a deterministic, factual fallback draft requiring no model service."""
    proof_points = content_config.get("proof_points", [])
    proof = str(proof_points[0]).strip() if proof_points else "The system creates locally reviewable production drafts."
    cta = rotation["cta"].format(website_url=content_config["website_url"])
    linkedin = f"{proof}\n\n{rotation['focus']}\n\n{cta}"
    prefix = f"{proof} {rotation['focus']}".replace("\n", " ").strip()
    x_post = f"{prefix[:max(0, 239 - len(cta))].rstrip()} {cta}".strip()
    return {"linkedin": linkedin[:1300], "x": x_post[:240]}


def run(run_date: date | None = None) -> dict[str, Any]:
    config = load_config()
    content_config = config["content"]
    timezone = ZoneInfo(content_config["timezone"])
    local_now = datetime.now(timezone)
    effective_date = run_date or local_now.date()
    if effective_date.weekday() not in ROTATION:
        return {"status": "skipped", "reason": "weekend", "content_date": effective_date.isoformat()}
    rotation = ROTATION[effective_date.weekday()]
    mode = content_config.get("mode", "template")
    if mode == "ollama":
        prompt = build_prompt(content_config, rotation)
        draft = generate(content_config["ollama_url"], content_config["ollama_model"], prompt)
        source = "local_ollama_endpoint"
        model = content_config["ollama_model"]
    elif mode == "template":
        draft = generate_template(content_config, rotation)
        source = "deterministic_local_template"
        model = "none"
    else:
        raise RuntimeError("content.mode must be either 'template' or 'ollama'.")
    content_dir = DATA_DIR / "content"
    content_dir.mkdir(parents=True, exist_ok=True)
    content_path = content_dir / f"{effective_date.isoformat()}-{rotation['type']}.json"
    record = {
        "status": "draft_ready",
        "content_date": effective_date.isoformat(),
        "generated_at": local_now.isoformat(),
        "timezone": content_config["timezone"],
        "post_type": rotation["type"],
        "source": source,
        "model": model,
        "publishing": "External publication is disabled; this file requires human review.",
        **draft,
    }
    database_path = Path(os.environ.get("ETHINX_DB", DATA_DIR / "ethinx.sqlite3"))
    store = SovereignStore(database_path, config)
    content_id = store.save_content(effective_date.isoformat(), rotation["type"], draft["linkedin"], draft["x"], str(content_path))
    content_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"status": "draft_ready", "content_id": content_id, "content_file": str(content_path), "content_date": effective_date.isoformat()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", type=date.fromisoformat, help="Override date for controlled testing, YYYY-MM-DD")
    args = parser.parse_args()
    result = run(args.date)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}))
        raise SystemExit(2)
