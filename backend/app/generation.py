"""LLM-based generation — local Ollama primary, Anthropic Claude backup.

Produces structured JSON for: Script, Scene plan, Metadata, Thumbnail concepts.
Falls back to deterministic generation if no LLM is available.
"""
from __future__ import annotations
from app.scene_terms import derive_search_terms, derive_visual_query
from app.visual_query import extract_visual_keywords, is_garbage_token, truncate_words
from app.timing import (
    MIN_SCRIPT_WORDS,
    NarrationPlan,
    TimingContract,
    normalize_scene_timeline,
    script_word_count,
    target_word_count,
)

import json
import logging
import os
from dotenv import load_dotenv
try:
    # Production configuration is supplied by systemd.  The repository .env
    # file is intentionally not readable by the service account, so it must
    # remain optional when this module is imported by the API router.
    load_dotenv()
except OSError:
    pass

import math
import random
import re
import time
import uuid
from pathlib import Path
from typing import Optional

import httpx

from . import observability as obs
from . import provider_usage

logger = logging.getLogger("facelessforge.generation")

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

# FIX 1: Strip BEAT markers and other structural labels from text paths.
# LLMs sometimes leak section labels like "BEAT 1", "Beat 2: ...", or
# "INTRO/OUTRO/CTA" into narration/caption text — these are internal
# scaffolding and must never reach the renderer (would burn into the video).
_BEAT_MARKER_RE = re.compile(r"BEAT\s*\d+", re.IGNORECASE)
_SECTION_LABEL_RE = re.compile(
    r"^\s*(HOOK|INTRO|OUTRO|CTA|SCENE\s*\d*|BEAT\s*\d*)\s*[:\-–—]\s*",
    re.IGNORECASE | re.MULTILINE,
)


def strip_beat_markers(text: str) -> str:
    """Remove BEAT markers and leading section labels from script text.

    Applied to every narration/caption path before it reaches the renderer.
    Safe to call on empty/None values.
    """
    if not text:
        return ""
    cleaned = _BEAT_MARKER_RE.sub("", text or "")
    cleaned = _SECTION_LABEL_RE.sub("", cleaned)
    # Collapse whitespace introduced by removals
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{2,}", "\n", cleaned)
    return cleaned.strip()


# Backwards-compat: also exported as `strip_beat_artifacts` for any importer
# that uses the old name.
strip_beat_artifacts = strip_beat_markers


UNSUPPORTED_TOOL_CLAIMS = ("zapier", "notion", "airtable", "canva", "airtasker")


def find_unsupported_tool_claims(script: dict, project: dict) -> list[str]:
    """Return third-party tools the model invented for an unrelated topic.

    The old script prompt actively told the model to name Zapier/Notion/etc.
    A generated script may only mention one of these when the project brief
    already names it. Manual scripts are user-authored and are not rewritten.
    """
    if not isinstance(script, dict):
        return []
    project = project or {}
    brief = project.get("structured_brief") or {}
    grounded = " ".join(str(value or "") for value in (
        project.get("topic"), project.get("niche"), project.get("audience"),
        project.get("name"), brief.get("service"), brief.get("offer"),
        brief.get("audience"), brief.get("location"), brief.get("problem"),
    )).lower()
    generated = " ".join(str(value or "") for value in (
        script.get("full_script"), script.get("selected_hook"),
        script.get("cta_block"),
        " ".join(str(x) for x in (script.get("retention_beats") or [])),
    )).lower()
    return [
        term for term in UNSUPPORTED_TOOL_CLAIMS
        if re.search(rf"\b{re.escape(term)}\b", generated)
        and not re.search(rf"\b{re.escape(term)}\b", grounded)
    ]


def _load_prompt_template(name: str) -> Optional[str]:
    """Load a prompt template from app/prompts/, None if missing/unreadable."""
    try:
        return (PROMPTS_DIR / name).read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("Prompt template %s unavailable: %s", name, e)
        return None


def _is_educational_topic(project: dict) -> bool:
    """Route pet / educational topics to the educational script template."""
    topic = (project.get("topic") or project.get("title") or "").lower()
    audience = (project.get("audience") or "").strip().lower()
    return True if any(k in (topic+" "+audience) for k in ["pet","dog","cat","mental","health","how much"]) else False


def _render_educational_prompt(project: dict, template: str, target_words: int) -> str:
    """Fill the educational template placeholders from the project."""
    filled = template
    replacements = {
        "{topic}": project.get("topic") or project.get("title") or "this subject",
        "{audience}": project.get("audience") or "general audience",
        "{tone}": project.get("tone") or "documentary",
        "{voice}": project.get("voice_style") or "neutral male narrator",
        "{visual}": project.get("visual_style") or "cinematic b-roll",
        "{cta}": project.get("cta") or "Subscribe for more videos like this.",
    }
    for key, value in replacements.items():
        filled = filled.replace(key, str(value))
    floor_words = script_word_floor(target_words)
    ceiling_words = script_word_ceiling(target_words)
    return filled + f"""

Respond with strict JSON in this exact shape:
{{
  "hook_option_one": "short curiosity hook under 15 words",
  "hook_option_two": "second hook option",
  "hook_option_three": "third hook option",
  "selected_hook": "the best hook from the three",
  "full_script": "THE FULL SCRIPT AS ONE SINGLE STRING. Do NOT use a list or array here.",
  "retention_beats": ["beat 1", "beat 2", "beat 3"],
  "cta_block": "call to action"
}}

CRITICAL: full_script must be ONE continuous string between {floor_words} and {ceiling_words} words (target {target_words}). Do not stop early and do not pad past the maximum. Count the words before responding."""


def _llm_available() -> bool:
    """Return True if any LLM provider is configured."""
    return bool(os.environ.get("OLLAMA_MODEL", "").strip()) or bool(
        os.environ.get("ANTHROPIC_API_KEY", "").strip()
    )


def _extract_json(text: str) -> Optional[dict]:
    """Extract the first JSON object from a string, stripping markdown fences."""
    if not text:
        return None
    text = text.strip()
    text = re.sub(r"^```json\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^```\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    # Extract first balanced JSON object
    match = re.search(r"(\{.*\})", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None



async def _try_deepseek_json(
    system: str, user: str, cache_key: str, max_tokens: int = 8192
) -> tuple[Optional[dict], str]:
    """Run one DeepSeek request and record measured usage, including failures."""
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not deepseek_key:
        return None, "DeepSeek not configured"
    started = time.monotonic()
    status_code: Optional[int] = None
    usage: dict = {}
    request_id: Optional[str] = None
    text = ""
    message = ""
    parsed: Optional[dict] = None
    try:
        logger.info("DeepSeek request for %s", cache_key)
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                "https://api.deepseek.com/chat/completions",
                headers={"Authorization": f"Bearer {deepseek_key}", "Content-Type": "application/json"},
                json={"model": "deepseek-chat", "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}], "temperature": 0.7, "max_tokens": max_tokens},
            )
        status_code = resp.status_code
        request_id = resp.headers.get("x-request-id")
        if status_code == 200:
            body = resp.json()
            usage = body.get("usage") or {}
            request_id = request_id or body.get("id")
            text = body["choices"][0]["message"]["content"]
            parsed = _extract_json(text)
            if parsed:
                logger.info("DeepSeek OK %s", cache_key)
                return parsed, ""
            message = f"DeepSeek non-JSON: {text[:200]}"
            logger.warning(message)
            return None, message
        message = f"DeepSeek {status_code}: {resp.text[:300]}"
        logger.warning(message)
        return None, message
    except Exception as e:  # noqa: BLE001 - provider failure falls through to existing backups
        message = f"DeepSeek {e}"
        logger.warning(message)
        obs.capture_exception(e, tags={
            "operation": "generation.llm",
            "stage": "provider_request",
            "terminal_status": "retrying",
            "provider": "deepseek",
            "model": "deepseek-chat",
            "error_code": "llm_provider_error",
            "attempt_number": 1,
            "retry_count": 0,
            "output_saved": False,
            "backend_outcome": "failed",
        })
        return None, message
    finally:
        await provider_usage.record_llm_attempt(
            provider="deepseek",
            model="deepseek-chat",
            cache_key=cache_key,
            success=parsed is not None,
            http_status=status_code,
            error_code=(
                None if parsed is not None
                else "llm_non_json" if status_code == 200
                else f"llm_http_{status_code}" if status_code
                else "llm_provider_error"
            ),
            error_message=message,
            duration_ms=int((time.monotonic() - started) * 1000),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            total_tokens=usage.get("total_tokens"),
            cached_tokens=usage.get("prompt_cache_hit_tokens"),
            prompt_characters=len(system or "") + len(user or ""),
            completion_characters=len(text or ""),
            request_id=request_id,
            metadata={"max_tokens": max_tokens, "temperature": 0.7},
        )


async def _llm_json(
    system: str, user: str, cache_key: str = "", max_tokens: int = 8192
) -> Optional[dict]:
    """Ollama PRIMARY, then DeepSeek, Kimi, Claude backup.

    Ollama is local, private, no API costs — always tried first.
    Cloud providers are fallbacks if Ollama is unavailable or returns non-JSON.
    """
    from dotenv import load_dotenv
    try:
        load_dotenv()
    except OSError:
        pass
    last_error: str = ""
    configured_provider = os.environ.get("LLM_PROVIDER", "").strip().lower()
    deepseek_attempted = False

    # Respect the active provider selection before trying local fallbacks.  The
    # production configuration selects DeepSeek; trying Ollama twice first
    # previously consumed two 120-second request budgets for one scene plan.
    if configured_provider in {"deepseek", "deepseek-chat"}:
        deepseek_attempted = True
        parsed, last_error = await _try_deepseek_json(system, user, cache_key, max_tokens=max_tokens)
        if parsed:
            return parsed

    # 1. Local Ollama (PRIMARY) — 2 attempts, 600s timeout per attempt
    ollama_model = os.environ.get("OLLAMA_MODEL", "llama3:latest").strip() or "llama3:latest"
    ollama_timeout = float(os.environ.get("OLLAMA_TIMEOUT_SECONDS", "600") or 600)
    ollama_url = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/generate").strip() or "http://localhost:11434/api/generate"
    # Support both /api/generate and /api/chat style URLs
    use_chat = ollama_url.rstrip("/").endswith("/chat")
    for attempt in range(2):
        try:
            logger.info("Ollama (%s) request for %s attempt %d", ollama_model, cache_key, attempt + 1)
            async with httpx.AsyncClient(timeout=ollama_timeout) as client:
                if use_chat:
                    resp = await client.post(
                        ollama_url,
                        json={
                            "model": ollama_model,
                            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                            "stream": False,
                            "format": "json",
                            "options": {"temperature": 0.7, "num_ctx": 8192, "num_predict": max_tokens},
                        },
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        text = data.get("message", {}).get("content", "") or data.get("response", "")
                        parsed = _extract_json(text)
                        if parsed:
                            logger.info("Ollama (%s) returned valid JSON for %s", ollama_model, cache_key)
                            return parsed
                        logger.warning("Ollama (%s) returned non-JSON for %s: %s", ollama_model, cache_key, text[:500])
                        last_error = "Ollama non-JSON response"
                    else:
                        logger.warning("Ollama non-200 %s %s - %s", resp.status_code, resp.reason_phrase, resp.text[:500])
                        last_error = f"Ollama HTTP {resp.status_code}"
                else:
                    resp = await client.post(
                        ollama_url,
                        json={
                            "model": ollama_model,
                            "system": system,
                            "prompt": user,
                            "stream": False,
                            "format": "json",
                            "options": {"temperature": 0.7, "num_ctx": 8192, "num_predict": max_tokens},
                        },
                    )
                    if resp.status_code == 200:
                        text = resp.json().get("response", "")
                        parsed = _extract_json(text)
                        if parsed:
                            logger.info("Ollama (%s) returned valid JSON for %s", ollama_model, cache_key)
                            return parsed
                        logger.warning("Ollama (%s) returned non-JSON for %s: %s", ollama_model, cache_key, text[:500])
                        last_error = "Ollama non-JSON response"
                    else:
                        logger.warning("Ollama non-200 %s %s - %s", resp.status_code, resp.reason_phrase, resp.text[:500])
                        last_error = f"Ollama HTTP {resp.status_code}"
        except httpx.TimeoutException as e:
            last_error = f"Ollama timeout ({ollama_timeout}s)"
            logger.warning("Ollama (%s) timeout for %s attempt %d: %s", ollama_model, cache_key, attempt + 1, e)
            obs.capture_exception(e, tags={
                "operation": "generation.llm",
                "stage": "provider_request",
                "terminal_status": "retrying",
                "provider": "ollama",
                "model": ollama_model,
                "error_code": "llm_timeout",
                "attempt_number": attempt + 1,
                "retry_count": attempt,
                "output_saved": False,
                "backend_outcome": "failed",
            })
        except Exception as e:
            last_error = f"Ollama {type(e).__name__}: {e}"
            logger.warning("Ollama (%s) call failed for %s attempt %d: %s", ollama_model, cache_key, attempt + 1, e)
            obs.capture_exception(e, tags={
                "operation": "generation.llm",
                "stage": "provider_request",
                "terminal_status": "retrying",
                "provider": "ollama",
                "model": ollama_model,
                "error_code": "llm_provider_error",
                "attempt_number": attempt + 1,
                "retry_count": attempt,
                "output_saved": False,
                "backend_outcome": "failed",
            })

    # 2. DeepSeek — cloud backup, unless it was the configured provider above.
    if not deepseek_attempted:
        parsed, last_error = await _try_deepseek_json(system, user, cache_key, max_tokens=max_tokens)
        if parsed:
            return parsed

    # 3. Kimi — second cloud backup
    kimi_key = os.environ.get("KIMI_API_KEY", "").strip() or os.environ.get("MOONSHOT_API_KEY", "").strip()
    if kimi_key:
        try:
            kimi_model = os.environ.get("KIMI_MODEL", "kimi-k2-0711-preview").strip() or "kimi-k2-0711-preview"
            logger.info("Kimi %s for %s", kimi_model, cache_key)
            async with httpx.AsyncClient(timeout=90.0) as client:
                resp = await client.post(
                    "https://api.moonshot.cn/v1/chat/completions",
                    headers={"Authorization": f"Bearer {kimi_key}", "Content-Type": "application/json"},
                    json={"model": kimi_model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}], "temperature": 0.7, "max_tokens": max_tokens},
                )
                if resp.status_code == 200:
                    text = resp.json()["choices"][0]["message"]["content"]
                    parsed = _extract_json(text)
                    if parsed:
                        logger.info("Kimi OK %s", cache_key)
                        return parsed
                last_error = f"Kimi {resp.status_code}"
        except Exception as e:
            last_error = f"Kimi {e}"
            obs.capture_exception(e, tags={
                "operation": "generation.llm",
                "stage": "provider_request",
                "terminal_status": "retrying",
                "provider": "kimi",
                "model": os.environ.get("KIMI_MODEL", "kimi-k2-0711-preview"),
                "error_code": "llm_provider_error",
                "attempt_number": 2,
                "retry_count": 1,
                "output_saved": False,
                "backend_outcome": "failed",
            })

    # 4. Claude — final backup (per skill: Claude is backup only)
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if anthropic_key:
        try:
            # NOTE: "claude-3-5-sonnet-latest" and "claude-3-5-sonnet-20241022" are
            # RETIRED (404 not_found). Use the pinned snapshot claude-sonnet-4-5-20250929.
            anthropic_model = os.environ.get("LLM_MODEL", "claude-sonnet-4-5-20250929").strip() or "claude-sonnet-4-5-20250929"
            logger.info("Claude %s for %s (backup)", anthropic_model, cache_key)
            async with httpx.AsyncClient(timeout=90.0) as client:
                resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={"x-api-key": anthropic_key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
                    json={"model": anthropic_model, "system": system, "messages": [{"role": "user", "content": user}], "max_tokens": max_tokens, "temperature": 0.7},
                )
                if resp.status_code == 200:
                    text = "".join([b.get("text", "") for b in resp.json().get("content", [])])
                    parsed = _extract_json(text)
                    if parsed:
                        logger.info("Claude OK %s", cache_key)
                        return parsed
                else:
                    last_error = f"Claude {resp.status_code}: {resp.text[:300]}"
                    logger.warning(last_error)
        except Exception as e:
            last_error = f"Claude {e}"
            obs.capture_exception(e, tags={
                "operation": "generation.llm",
                "stage": "provider_request",
                "terminal_status": "failed",
                "provider": "claude",
                "model": anthropic_model,
                "error_code": "llm_provider_error",
                "attempt_number": 3,
                "retry_count": 2,
                "output_saved": False,
                "backend_outcome": "failed",
            })

    logger.error(f"All LLMs failed {cache_key}: {last_error}")
    obs.capture_message(f"All LLMs failed {cache_key}: {last_error}", level="error",
                        fingerprint=["generation", "llm_exhausted"],
                        tags={
                            "operation": "generation.llm",
                            "stage": "provider_chain_exhausted",
                            "terminal_status": "failed",
                            "provider": configured_provider or "ollama",
                            "model": ollama_model,
                            "error_code": "llm_unavailable",
                            "attempt_number": 4,
                            "retry_count": 3,
                            "output_saved": False,
                            "backend_outcome": "failed",
                        })
    return None


# ------------------------------ SCRIPT ------------------------------

SCRIPT_SYSTEM = (
    "You are a real business owner talking to a friend. Use contractions, short sentences, 8th grade words. No AI phrases. Start with a personal story. "
    "Never invent software, tools, integrations, brands, or product names that the user did not provide. "
    "Always respond with strict JSON, no commentary. All text in English. "
    "Deliver at the requested length: text far below the requested word count is treated as a failed generator response, not a finished script."
)

# Scripts are planned from the same duration contract as voiceover validation
# and render preflight. The bounds are a planning envelope, not a promise that
# the selected voice will land at 150 wpm; the measured take is the authority.
SCRIPT_WORD_FLOOR_RATIO = 0.95
SCRIPT_WORD_CEILING_RATIO = 1.05
CHUNK_WORD_TARGET = 2500


class ScriptLengthError(Exception):
    """A generated AI script is outside the narration planning envelope.

    Carries the counts so callers can surface an explicit, repairable error
    instead of silently truncating narration or paying for a take that cannot
    fit the selected duration.
    """

    def __init__(self, *, words: int, floor: int, ceiling: int | None = None):
        self.word_count = int(words)
        self.word_floor = int(floor)
        self.word_ceiling = int(ceiling) if ceiling is not None else None
        if self.word_ceiling is not None and self.word_count > self.word_ceiling:
            message = (
                f"AI script measured {self.word_count} words against a "
                f"{self.word_floor}-{self.word_ceiling} word range. "
                "Shorten or regenerate the script."
            )
        else:
            message = (
                f"AI script measured {self.word_count} words against a "
                f"{self.word_floor}-word minimum. Lengthen or regenerate the script."
            )
        super().__init__(message)


class ScriptUnderLengthError(ScriptLengthError):
    """Back-compat name for the short-script case."""


def _count_script_words(text: str) -> int:
    """Word count with the same tokenizer _finalize_script reports."""
    return script_word_count(text)


def script_word_floor(target_words: int) -> int:
    """Minimum word count a script must reach for its narration budget."""
    try:
        target = int(target_words or 0)
    except (TypeError, ValueError):
        target = 0
    if target <= 0:
        return MIN_SCRIPT_WORDS
    return max(MIN_SCRIPT_WORDS, math.ceil(target * SCRIPT_WORD_FLOOR_RATIO))


def script_word_ceiling(target_words: int) -> int:
    """Maximum word count allowed by the same narration planning envelope."""
    try:
        target = int(target_words or 0)
    except (TypeError, ValueError):
        target = 0
    if target <= 0:
        return MIN_SCRIPT_WORDS
    return max(script_word_floor(target), math.floor(target * SCRIPT_WORD_CEILING_RATIO))


def script_output_token_budget(target_words: int) -> int:
    """Scale the provider output budget to the requested narration length.

    The old fixed 4k budget silently cut long scripts mid-sentence. 1.65
    tokens/word leaves room for punctuation and the JSON envelope; chunked
    generation keeps each individual request inside provider limits.
    """
    try:
        words = max(1, int(target_words or 1))
    except (TypeError, ValueError):
        words = 1
    return max(2048, min(8192, math.ceil(words * 1.8) + 512))


# Small shortfalls are closed with a neutral, complete sentence rather than a
# paid rewrite that may overshoot. Existing narration is preserved verbatim.
MICRO_CONTINUATIONS = {
    1: "Indeed.",
    2: "That matters.",
    3: "That is it.",
    4: "That is the point.",
    5: "That is the practical point.",
    6: "That is the practical lesson here.",
    7: "That is the practical lesson to keep.",
}


def micro_continuation(
    current_words: int, *, min_words: int, max_words: int, target_words: int
) -> str | None:
    """Return a short closing sentence that lands inside the word envelope."""
    need = max(1, min_words - current_words)
    room = max(1, max_words - current_words)
    preferred = max(need, min(room, target_words - current_words))
    for count in range(preferred, room + 1):
        phrase = MICRO_CONTINUATIONS.get(count)
        if phrase:
            return phrase
    for count in range(need, room + 1):
        phrase = MICRO_CONTINUATIONS.get(count)
        if phrase:
            return phrase
    return None


async def _generate_script_chunk(
    system: str,
    topic: str,
    part_idx: int,
    total_parts: int,
    words_this_chunk: int,
    cache_key: str,
    max_tokens: int = 8192,
):
    part_intro = f"PART {part_idx}/{total_parts} of script about {topic}. "
    if total_parts > 1:
        if part_idx == 1:
            part_intro += f"OPENING - write {words_this_chunk} words, hook to continue. "
        elif part_idx == total_parts:
            part_intro += f"FINAL - write {words_this_chunk} words, wrap up + CTA. "
        else:
            part_intro += f"MIDDLE part {part_idx} - write {words_this_chunk} words, continue. "
    floor_this = max(MIN_SCRIPT_WORDS, math.ceil(words_this_chunk * 0.7))
    user = part_intro + f"\nCRITICAL: Write at least {floor_this} words for this chunk."
    return await _llm_json(
        system, user, f"{cache_key}-part{part_idx}", max_tokens=max_tokens
    )


async def generate_long_script(
    project: dict, target_words: int, base_system: str, base_cache_key: str
):
    """Generate a long script in bounded chunks, or fail closed.

    A chunk that produced no script is never silently skipped; the stitched
    result must also stay inside the shared planning envelope. Returning
    ``None`` lets the caller fall back to the gated single-shot path.
    """
    words_per_chunk = CHUNK_WORD_TARGET
    total_parts = math.ceil(target_words / words_per_chunk)
    full_parts: list[str] = []
    first = None
    for i in range(1, total_parts + 1):
        this_words = min(words_per_chunk, target_words - (i - 1) * words_per_chunk)
        if this_words <= 0:
            break
        res = await _generate_script_chunk(
            base_system,
            project.get("topic") or "",
            i,
            total_parts,
            this_words,
            base_cache_key,
            max_tokens=script_output_token_budget(this_words),
        )
        if not res or not res.get("full_script"):
            logger.warning(
                "Long-form chunk %s/%s produced no script; failing closed", i, total_parts
            )
            return None
        if first is None:
            first = res
        full_parts.append(res.get("full_script", "").strip())
    if not full_parts:
        return None
    stitched = "\n\n".join(full_parts)
    words = _count_script_words(stitched)
    floor = script_word_floor(target_words)
    ceiling = script_word_ceiling(target_words)
    if words < floor or words > ceiling:
        logger.warning(
            "Long-form script measured %s words outside %s-%s; failing closed",
            words,
            floor,
            ceiling,
        )
        return None
    first["full_script"] = stitched
    return first


def _coerce_script(result: dict) -> dict:
    """Normalize a parsed script JSON into the expected schema.
    All text fields are scrubbed of BEAT markers and structural labels."""
    if not isinstance(result, dict):
        return None
    raw_fs = result.get("full_script")
    fs_text = ""
    if isinstance(raw_fs, str):
        fs_text = strip_beat_markers(raw_fs)
    elif isinstance(raw_fs, list):
        # Some models return a list of {time, text} objects or plain strings
        parts = []
        for item in raw_fs:
            if isinstance(item, dict):
                parts.append(strip_beat_markers(str(item.get("text") or "")))
            elif isinstance(item, str):
                parts.append(strip_beat_markers(item))
        fs_text = " ".join(parts)
    out = {
        "hook_option_one": strip_beat_markers(str(result.get("hook_option_one") or "")),
        "hook_option_two": strip_beat_markers(str(result.get("hook_option_two") or "")),
        "hook_option_three": strip_beat_markers(str(result.get("hook_option_three") or "")),
        "selected_hook": strip_beat_markers(str(result.get("selected_hook") or "")),
        "full_script": fs_text,
        "retention_beats": [],
        "cta_block": strip_beat_markers(str(result.get("cta_block") or "")),
    }
    beats = result.get("retention_beats")
    if isinstance(beats, list):
        out["retention_beats"] = [strip_beat_markers(str(b)) for b in beats if b]
    return out


def _finalize_script(out: dict, narration_duration: float = None) -> dict:
    """Attach derived stats; never rewrite or truncate narration here.

    Length is reported as data. Long AI output is corrected by the bounded
    generator path (or rejected), and manual text is preserved verbatim. The
    paid voiceover preflight is the final gate before synthesis.
    """
    full = out.get("full_script") or ""
    words = script_word_count(full)
    out["word_count"] = words
    out["estimated_duration"] = int(words / 2.5)
    # Duration-awareness: record the word envelope the narration called for so
    # shortfalls/overruns are visible everywhere a script doc is stored.
    try:
        budget = target_word_count(float(narration_duration)) if narration_duration else None
    except Exception:
        budget = None
    out["word_budget"] = budget
    out["budget_coverage"] = round(words / budget, 3) if budget else None
    if budget:
        floor_words = script_word_floor(budget)
        ceiling_words = script_word_ceiling(budget)
        out["word_floor"] = floor_words
        out["word_ceiling"] = ceiling_words
        if words < floor_words:
            status = "too_short"
            out["under_length"] = True
            out["length_message"] = (
                f"Script has {words} words; {budget} words is the planning target "
                f"and {floor_words} is the minimum. Add about {floor_words - words} words."
            )
        elif words > ceiling_words:
            status = "too_long"
            out["under_length"] = False
            out["length_message"] = (
                f"Script has {words} words; {ceiling_words} is the planning maximum "
                f"for this target. Remove about {words - ceiling_words} words."
            )
        else:
            status = "within_bounds"
            out["under_length"] = False
            out["length_message"] = (
                f"Script has {words} words; the planning range is "
                f"{floor_words}-{ceiling_words}."
            )
        out["length_status"] = status
        out["within_bounds"] = status == "within_bounds"
    else:
        out["word_floor"] = None
        out["word_ceiling"] = None
        out["under_length"] = None
        out["length_status"] = "unknown"
        out["within_bounds"] = None
        out["length_message"] = "No narration duration was supplied for length validation."
    return out


async def generate_script(project: dict) -> dict:
    # EDUCATIONAL routing - use educational_script.txt for pet/health topics.
    # These topics MUST never run through the generic business origin-story prompt.
    _topic_l = (project.get("topic") or project.get("title") or "").lower()
    _aud_l = (project.get("audience") or "").lower()
    _haystack = f"{_topic_l} {_aud_l}"
    _edu_keys = ["pet", "dog", "cat", "mental", "health", "how much", "whether",
                 "anxiety", "depression", "wellbeing", "oxytocin", "pet ownership"]
    _is_edu = any(k in _haystack for k in _edu_keys)

    target_dur = int(project.get("target_duration", 300))
    plan = NarrationPlan.from_project(project)
    narration_dur = plan.narration_seconds
    target_words = plan.words_target
    min_words = plan.words_min
    max_words = plan.words_max
    output_tokens = script_output_token_budget(target_words)
    base_cache_key = f"script-{project.get('id', uuid.uuid4())}"

    current_finalized: Optional[dict] = None
    current_text = ""
    current_words = 0
    current_status: Optional[str] = None
    best_finalized: Optional[dict] = None
    best_words = 0
    best_status: Optional[str] = None

    def _track(finalized: dict) -> Optional[dict]:
        nonlocal current_finalized, current_text, current_words, current_status
        nonlocal best_finalized, best_words, best_status
        current_finalized = finalized
        current_text = str(finalized.get("full_script") or "")
        current_words = int(finalized.get("word_count") or 0)
        current_status = str(finalized.get("length_status") or "too_short")
        if current_status == "within_bounds":
            return finalized
        if best_finalized is None or abs(current_words - target_words) < abs(best_words - target_words):
            best_finalized = finalized
            best_words = current_words
            best_status = current_status
        return None

    def _accept_response(result: Optional[dict]) -> Optional[dict]:
        if not result:
            return None
        coerced = _coerce_script(result)
        if not coerced or not coerced.get("full_script", "").strip():
            return None
        unsupported = find_unsupported_tool_claims(coerced, project)
        if unsupported:
            logger.warning(
                "Discarding generated script with unsupported tool claims: %s",
                unsupported,
            )
            return None
        finalized = _finalize_script(coerced, narration_dur)
        return _track(finalized)

    if target_words > 3000:
        logger.info(
            "Long form %s words -> chunked %s parts",
            target_words,
            math.ceil(target_words / CHUNK_WORD_TARGET),
        )
        try:
            long_res = await generate_long_script(
                project, target_words, SCRIPT_SYSTEM, base_cache_key
            )
            if long_res and long_res.get("full_script"):
                accepted = _accept_response(long_res)
                if accepted:
                    return accepted
        except Exception as e:
            logger.warning("Chunked long-form failed, falling back to single: %s", e)

    if _is_edu:
        logger.info(
            "EDUCATIONAL topic detected: %s -> using educational_script.txt",
            project.get("topic"),
        )
        tmpl = _load_prompt_template("educational_script.txt")
        if tmpl:
            user = _render_educational_prompt(project, tmpl, target_words)
            result = await _llm_json(
                SCRIPT_SYSTEM,
                user,
                f"script-{project.get('id','edu')}",
                max_tokens=output_tokens,
            )
            accepted = _accept_response(result)
            if accepted:
                return accepted
            logger.warning(
                "Educational template LLM failed - using on-topic deterministic fallback"
            )
        # Educational topics never fall through to the generic business prompt.
    else:
        user = f"""Generate a faceless YouTube script as strict JSON with this exact shape:
{{
  "hook_option_one": "short curiosity hook under 15 words",
  "hook_option_two": "second hook option",
  "hook_option_three": "third hook option",
  "selected_hook": "the best hook from the three",
  "full_script": "THE FULL SCRIPT AS ONE SINGLE STRING. Do NOT use a list or array here.",
  "retention_beats": ["beat 1", "beat 2", "beat 3"],
  "cta_block": "call to action"
}}

Topic: {project.get('topic', project.get('title', 'business success story'))}
Niche: {project.get('niche', 'business')}
Target duration: {target_dur}s. Narration slot: {narration_dur:.1f}s.
Word target: {target_words}. Acceptable range: {min_words}-{max_words} words.
Tone: {project.get('tone', 'documentary')}

CRITICAL: full_script must be ONE continuous string between {min_words} and {max_words} words. Do not stop early, and do not pad past the maximum. Count the words before responding. A script outside this range is rejected and corrected.
Write specifically about the Topic above. Do NOT fall back to a generic entrepreneur/startup origin story (instant noodles, sleeping in the office, investors saying no, first 100 customers) unless the Topic explicitly asks for one.
Do not include BEAT markers, section headers, or structural labels (such as "BEAT 1", "INTRO:", "HOOK:", "CTA:", "SCENE 1:", etc.) in the output. All text must read as continuous voiceover."""

        result = await _llm_json(
            SCRIPT_SYSTEM, user, base_cache_key, max_tokens=output_tokens
        )
        accepted = _accept_response(result)
        if accepted:
            return accepted

    # Bounded text correction. Small shortfalls are closed deterministically
    # with a complete closing sentence; larger shortfalls use a continuation
    # request that preserves the previous narration verbatim. Overlong takes
    # get a complete condensation request. No TTS is called here and no text is
    # ever silently truncated.
    for correction_round in (1, 2):
        if current_finalized is None or current_status is None:
            break
        if current_status == "within_bounds":
            return current_finalized
        if current_status == "too_short":
            micro = micro_continuation(
                current_words, min_words=min_words, max_words=max_words,
                target_words=target_words,
            )
            if micro:
                combined = f"{current_text} {micro}".strip()
                finalized = _finalize_script(
                    {**(current_finalized or {}), "full_script": combined},
                    narration_dur,
                )
                accepted = _track(finalized)
                if accepted:
                    logger.info(
                        "Script length closed with a deterministic %s-word continuation "
                        "(%s -> %s words)",
                        script_word_count(micro), current_words,
                        finalized.get("word_count"),
                    )
                    return accepted
                # A micro closer cannot make an overlong take shorter.
                break

            add_words = max(1, min(target_words - current_words, max_words - current_words))
            need_words = max(1, min_words - current_words)
            room_words = max(1, max_words - current_words)
            retry_user = f"""The complete narration below is {current_words} words. It needs to be {min_words}-{max_words} words (target {target_words}).
Return STRICT JSON only: {{"continuation": "..."}} where continuation is ONE natural closing passage of exactly {add_words} words (minimum {need_words}, maximum {room_words}) that concludes the narration without changing its meaning.
Do NOT return the full script, do NOT return a full_script field, do NOT repeat, shorten, rewrite, summarise or explain the existing narration. Only the new closing words belong in the continuation value. A response at or near the length of the existing narration is a shape violation, is discarded and cannot be appended.

EXISTING NARRATION:
{current_text}

Topic: {project.get('topic', project.get('title', 'this subject'))}
Niche: {project.get('niche', 'business')}
Tone: {project.get('tone', 'documentary')}"""
        else:
            overage = max(1, current_words - max_words)
            retry_user = f"""The complete narration below is {current_words} words, which is {overage} words over the {max_words}-word maximum.
Rewrite the COMPLETE narration at no more than {max_words} words (target {target_words}). Preserve the meaning and the strongest concrete details; remove repetition and secondary examples. Return the full narration as full_script, not a fragment or only the removed text.

PREVIOUS NARRATION:
{current_text}

Topic: {project.get('topic', project.get('title', 'this subject'))}"""

        result = await _llm_json(
            SCRIPT_SYSTEM,
            retry_user,
            f"{base_cache_key}-length-{correction_round}",
            max_tokens=output_tokens,
        )
        if current_status == "too_short":
            raw = result or {}
            continuation = str(raw.get("continuation") or "").strip()
            used_key = "continuation"
            if not continuation:
                # Legacy/defensive path: a provider that ignores the fragment schema.
                continuation = str((_coerce_script(raw) or {}).get("full_script") or "").strip()
                used_key = "full_script"
            if continuation:
                cont_words = script_word_count(continuation)
                echoes_narration = bool(current_text.strip()) and (
                    continuation.startswith(current_text.strip()[:80])
                    or current_text.strip()[:80] in continuation
                    or current_text.strip() in continuation
                )
                if used_key != "continuation" or echoes_narration:
                    logger.warning(
                        "Continuation correction round %s rejected: fragment path returned a full rewrite "
                        "(%s words, key=%s); a %s-%s word continuation was required",
                        correction_round, cont_words, used_key, need_words, room_words,
                    )
                    continue
                room = max(1, max_words - current_words)
                need = max(1, min_words - current_words)
                if need <= cont_words <= room:
                    combined = f"{current_text} {continuation}".strip()
                    finalized = _finalize_script(
                        {**(current_finalized or {}), "full_script": combined},
                        narration_dur,
                    )
                    accepted = _track(finalized)
                    if accepted:
                        return accepted
                logger.warning(
                    "Continuation correction round %s returned %s words; needed %s-%s",
                    correction_round, cont_words, need, room,
                )
        else:
            accepted = _accept_response(result)
            if accepted:
                return accepted

    if current_finalized is None:
        # LLM outage path: deterministic on-topic skeleton, never a canned
        # unrelated story. The paid voiceover preflight still enforces length.
        topic = (project.get("topic") or project.get("title") or "this subject").strip().rstrip("?")
        if _is_edu:
            return _finalize_script({
                "hook_option_one": f"What the research really says about {topic}",
                "hook_option_two": f"The science behind {topic} most people miss",
                "hook_option_three": f"How {topic} actually affects us - the evidence",
                "selected_hook": f"What the research really says about {topic}",
                "full_script": (
                    f"Today we are looking at what the research actually says about {topic}. "
                    "This is not a story about a startup or a founder - it is about evidence. "
                    "Studies consistently show that the way we live and the creatures we share "
                    "our lives with shape our mental and physical health in measurable ways. "
                    "Different pet types - dogs, cats, fish, birds - have different effects, "
                    "and the mechanisms behind them, from oxytocin release to routine and "
                    "social connection, are now well documented. "
                    "The key insight is that small daily changes compound over time. "
                    "Data beats guesswork, and the research gives us a clear picture of what "
                    "actually helps. "
                    "So here is the takeaway: understand the science, apply the evidence, "
                    "and build habits that genuinely support your wellbeing."
                ),
                "retention_beats": [
                    "What the studies show",
                    "Dog vs cat vs fish - the differences",
                    "The oxytocin and mental-health mechanism",
                    "Practical takeaways you can use today",
                ],
                "cta_block": "If this was useful, subscribe. New evidence-based videos every week.",
            }, narration_dur)
        return _finalize_script({
            "hook_option_one": f"What nobody tells you about {topic}",
            "hook_option_two": f"The truth about {topic} most people miss",
            "hook_option_three": f"Why {topic} matters more than you think",
            "selected_hook": f"What nobody tells you about {topic}",
            "full_script": (
                f"This video is about {topic}. "
                "In the next few minutes, you will learn what really drives it, "
                "why most people misunderstand it, and what you can do differently starting today. "
                f"Let's begin with the fundamentals. When people first encounter {topic}, "
                "they usually focus on the surface and miss the mechanisms underneath. "
                "That is exactly where the advantage hides. "
                "The first key insight is that small, consistent decisions compound. "
                "What looks like overnight success is almost always the visible tip of a long, deliberate process. "
                "The second insight is that data beats guesswork. "
                "The people who win in this space measure what works, cut what does not, and iterate fast. "
                "The third insight is that distribution matters as much as quality. "
                "Even the best work fails in silence if nobody sees it. "
                "So here is the takeaway: understand the fundamentals, measure everything, and ship consistently. "
                "Do that, and the results stop being luck and start being math."
            ),
            "retention_beats": [
                "The fundamentals most people skip",
                "Why data beats guesswork",
                "The distribution mistake that kills good work",
            ],
            "cta_block": "If this was useful, subscribe. New videos like this every week.",
        }, narration_dur)

    final_words = int((best_finalized or current_finalized or {}).get("word_count") or current_words)
    final_status = best_status or current_status or "too_short"
    logger.error(
        "AI script outside planning envelope after correction: %s words outside %s-%s (topic=%r)",
        final_words,
        min_words,
        max_words,
        project.get("topic"),
    )
    obs.capture_message(
        f"AI script outside planning envelope after correction: {final_words} outside {min_words}-{max_words}",
        level="error",
        fingerprint=["generation", "script_length"],
        tags={
            "operation": "generation.script",
            "stage": "word_length_gate",
            "terminal_status": "failed",
            "provider": os.environ.get("LLM_PROVIDER", "") or "ollama",
            "model": os.environ.get("OLLAMA_MODEL", "llama3:latest"),
            "error_code": "script_length_mismatch",
            "attempt_number": 3,
            "retry_count": 2,
            "output_saved": False,
            "requested_duration_seconds": target_dur,
            "scene_count": 0,
            "backend_outcome": "failed",
        },
    )
    raise ScriptLengthError(
        words=final_words,
        floor=min_words,
        ceiling=max_words,
    )


# ------------------------------ SCENE PLAN ------------------------------

SCENE_SYSTEM = (
    "You are a video editor planning scene breakdowns for faceless YouTube videos. "
    "Always respond with strict JSON. No commentary."
)


def _clean_search_terms(terms: list, narration: str, *, min_terms: int = 4) -> list[str]:
    """Filter LLM search_terms down to real stock-video query terms and
    top up from the narration until we hold at least ``min_terms``.

    Drops garbage tokens like "STARTUPSTARTUPSTARTU" (repeat artifacts) via
    ``is_garbage_token`` — a term is kept only if every word in it is clean.
    """
    out: list[str] = []
    for t in terms:
        t = " ".join(str(t or "").split()).strip()
        if not t:
            continue
        words = t.lower().split()
        if any(is_garbage_token(w) for w in words):
            continue
        if t.lower() in (x.lower() for x in out):
            continue
        out.append(t)
    if len(out) < min_terms:
        for kw in extract_visual_keywords(narration, top_n=min_terms):
            if len(out) >= min_terms:
                break
            if not any(kw in x.lower() for x in out):
                out.append(kw)
    return out[:6]


def _coerce_scenes(scenes: list) -> list[dict]:
    """Normalize scene objects to the expected schema. All text fields are
    scrubbed of BEAT markers and structural labels before reaching the renderer."""
    out = []
    for s in scenes:
        if not isinstance(s, dict):
            continue
        raw_narration = s.get("narration_text") or s.get("caption_text") or ""
        raw_caption = s.get("caption_text") or s.get("narration_text") or ""
        narration = strip_beat_markers(str(raw_narration))
        caption = strip_beat_markers(str(raw_caption))
        search_terms = _clean_search_terms(s.get("search_terms") or [], narration)
        out.append({
            "scene_number": int(s.get("scene_number") or 0) or len(out) + 1,
            "start_time": float(s.get("start_time") or 0),
            "end_time": float(s.get("end_time") or 0),
            "duration": float(s.get("duration") or 0),
            "narration_text": narration,
            "visual_direction": str(s.get("visual_direction") or ""),
            # Word-boundary cut at 80 chars — never mid-word, never a
            # hard [:120] slice that mangles the phrase.
            "caption_text": truncate_words(caption, 80),
            "search_terms": search_terms,
        })
    return out


async def generate_scenes(project: dict, full_script: str) -> list[dict]:
    """Back-compat wrapper used by routes.generate_scenes_endpoint — adapts
    the (project, full_script: str) call signature to ``generate_scene_plan``.
    """
    return await generate_scene_plan(project, {"full_script": full_script or ""})


def _split_script_exact(full_script: str, scene_count: int) -> list[str]:
    """Split narration into scene chunks without changing a single word.

    Scene planning is semantic only: the model may choose visuals and search
    terms, but it must never rewrite the voiceover.  Concatenating the returned
    chunks with single spaces reproduces the whitespace-normalised script.
    """
    count = max(1, int(scene_count))
    text = " ".join(str(full_script or "").split())
    if not text:
        return [""] * count
    sentences = re.split(r'(?<=[.!?])\s+', text)
    if len(sentences) >= count:
        chunks: list[str] = []
        start = 0
        remaining_chars = len(text)
        for index in range(count):
            remaining_scenes = count - index
            if remaining_scenes == 1:
                chunks.append(" ".join(sentences[start:]))
                break
            target = remaining_chars / remaining_scenes
            group: list[str] = []
            group_len = 0
            while start < len(sentences):
                candidate = sentences[start]
                if group and group_len + len(candidate) + 1 > target and len(sentences) - start >= remaining_scenes - 1:
                    break
                group.append(candidate)
                group_len += len(candidate) + 1
                start += 1
            chunks.append(" ".join(group))
            remaining_chars -= group_len
        return chunks

    # Fewer sentences than scenes: split by words, preferring punctuation
    # boundaries near the balanced target.
    words = text.split(" ")
    chunks = []
    start = 0
    total = len(words)
    for index in range(count):
        remaining_scenes = count - index
        if remaining_scenes == 1:
            chunks.append(" ".join(words[start:]))
            break
        target = (total - start) / remaining_scenes
        end = start + max(1, int(round(target)))
        best = end
        for delta in range(0, 5):
            for candidate in (end - delta, end + delta):
                if start < candidate < total and words[candidate - 1].endswith((".", "!", "?", ";", ":")):
                    best = candidate
                    break
            else:
                continue
            break
        # Clamp inside the remaining words.  A short script can request more
        # scenes than it has words; the old upper bound went negative there
        # and produced an IndexError instead of empty padding chunks.
        if start >= total:
            chunks.append("")
            continue
        best = min(max(best, start + 1), total)
        chunks.append(" ".join(words[start:best]))
        start = best
    return chunks


async def generate_scene_plan(project: dict, script: dict,
                              trace: Optional[dict] = None) -> list[dict]:
    """Plan scenes and record whether the deterministic fallback was used.

    ``trace`` is an optional caller-owned dict.  The API route persists it as
    ``project.scene_generation`` so Sentry and the UI can distinguish a real
    LLM plan from a silent fallback.  Keeping the return type as ``list[dict]``
    preserves every existing caller.
    """
    if trace is None:
        trace = {}
    trace.clear()
    trace.update({
        "fallback_used": False,
        "fallback_reason": None,
        "error_code": None,
        "llm_scene_count": 0,
        "scene_count": 0,
        "requested_duration_seconds": int(project.get("target_duration", 300) or 300),
    })
    target_dur = int(project.get("target_duration", 300))
    timing = TimingContract.from_project(project)
    narration_dur = timing.narration_seconds
    # The model decides semantics only. Code owns all frame timestamps.
    scene_duration = 30
    scene_count = max(1, math.ceil(narration_dur / scene_duration))
    full_script = script.get("full_script", "")
    if not isinstance(full_script, str):
        full_script = ""
    exact_chunks = _split_script_exact(full_script, scene_count)

    user = f"""Break this script into {scene_count} semantic scenes for a {narration_dur}s narration inside a {target_dur}s faceless YouTube video.
Do not calculate timestamps. The application assigns an exact frame budget after generation.
Return strict JSON with this exact shape:
{{"scenes": [
  {{
    "scene_number": 1,
    "start_time": 0.0,
    "end_time": {scene_duration}.0,
    "duration": {scene_duration}.0,
    "narration_text": "exact script excerpt for this scene",
    "visual_direction": "what stock footage to show — be specific to the topic",
    "caption_text": "key phrase for caption overlay",
    "search_terms": ["term1", "term2", "term3"]
  }}
]}}

The visual_direction and search_terms MUST be specific to the topic, not generic business footage.
You MUST generate exactly {scene_count} scenes - no more, no less.

Do not include BEAT markers, section headers, or structural labels (such as "BEAT 1", "INTRO:", "HOOK:", "SCENE 1:", etc.) in narration_text or caption_text. Text must read as continuous voiceover.

Script: {full_script[:3000]}
Topic: {project.get('topic', project.get('title', ''))}
Niche: {project.get('niche', 'business')}"""

    result = await _llm_json(SCENE_SYSTEM, user, f"scenes-{project.get('id', uuid.uuid4())}")
    if not result or not isinstance(result.get("scenes"), list):
        trace["fallback_used"] = True
        trace["fallback_reason"] = "scene_parse_failed"
        trace["error_code"] = "scene_parse_failed"
        logger.warning("Scene plan LLM returned no parsable scenes project=%s", project.get("id"))
    if result and isinstance(result.get("scenes"), list):
        trace["llm_scene_count"] = len(result["scenes"])
        coerced = _coerce_scenes(result["scenes"])
        if coerced:
            # Enforce semantic count, then discard every model-supplied timestamp.
            if len(coerced) != scene_count:
                logger.warning("LLM scene count mismatch: got %s expected %s, slicing/padding", len(coerced), scene_count)
                trace["fallback_used"] = True
                trace["fallback_reason"] = "scene_count_mismatch"
                trace["error_code"] = "scene_count_mismatch"
                if len(coerced) > scene_count:
                    coerced = coerced[:scene_count]
                else:
                    while len(coerced) < scene_count:
                        idx = len(coerced)
                        start = idx * scene_duration
                        coerced.append({
                            "scene_number": idx + 1,
                            "start_time": float(start),
                            "end_time": float(start + scene_duration),
                            "duration": float(scene_duration),
                            "narration_text": "",
                            "visual_direction": "",
                            "caption_text": "",
                            "search_terms": [],
                        })
            # Code owns narration: model text may paraphrase or invent claims.
            # Keep its visual planning, but lock every word to the approved script.
            for idx, scene in enumerate(coerced):
                scene["narration_text"] = exact_chunks[idx]
                # Captions are viewer-facing too: derive them from the approved
                # narration rather than a model paraphrase.
                first_sentence = re.split(r'(?<=[.!?])\s+', exact_chunks[idx])[0]
                scene["caption_text"] = truncate_words(first_sentence, 80)
                if not scene.get("visual_direction"):
                    scene["visual_direction"] = f"stock b-roll illustrating: {exact_chunks[idx][:80]}"
                if not scene.get("search_terms"):
                    scene["search_terms"] = derive_search_terms(exact_chunks[idx], idx, scene_count)
            joined = " ".join(scene["narration_text"] for scene in coerced)
            expected = " ".join(full_script.split())
            if joined != expected:
                logger.warning("LLM scene narration did not preserve script; using exact fallback split")
                trace["fallback_used"] = True
                trace["fallback_reason"] = "scene_narration_mismatch"
                trace["error_code"] = "scene_narration_mismatch"
            else:
                planned = normalize_scene_timeline(
                    coerced,
                    narration_frames=timing.narration_frames,
                    fps=timing.fps,
                )
                trace["scene_count"] = len(planned)
                return planned

    # Deterministic fallback — visuals derived from each scene's exact
    # narration chunk, never from the raw project topic/description text.
    if not trace.get("fallback_used"):
        trace["fallback_used"] = True
        trace["fallback_reason"] = "scene_parse_failed"
        trace["error_code"] = "scene_parse_failed"
    scene_dur = narration_dur / scene_count
    scenes = []
    for i, narration in enumerate(exact_chunks):
        start = i * scene_dur
        terms = derive_search_terms(narration, i, scene_count)
        scenes.append({
            "scene_number": i + 1,
            "start_time": round(start, 1),
            "end_time": round(start + scene_dur, 1),
            "duration": round(scene_dur, 1),
            "narration_text": narration,
            "visual_direction": f"stock b-roll illustrating: {terms[0]}",
            "caption_text": truncate_words(narration, 80) or f"Part {i + 1}",
            "search_terms": terms,
        })
    planned = normalize_scene_timeline(
        scenes,
        narration_frames=timing.narration_frames,
        fps=timing.fps,
    )
    trace["scene_count"] = len(planned)
    obs.capture_message(
        f"Scene plan fell back to deterministic split: {trace.get('fallback_reason')}",
        level="warning", fingerprint=["generation", "scene_fallback"],
        tags={
            "operation": "generation.scenes",
            "stage": "fallback",
            "terminal_status": "degraded",
            "provider": os.environ.get("LLM_PROVIDER", "") or "ollama",
            "model": os.environ.get("OLLAMA_MODEL", "llama3:latest"),
            "error_code": trace.get("error_code") or "scene_parse_failed",
            "attempt_number": 2,
            "retry_count": 1,
            "output_saved": False,
            "requested_duration_seconds": target_dur,
            "scene_count": len(planned),
            "backend_outcome": "degraded",
        })
    return planned


# ------------------------------ METADATA ------------------------------

META_SYSTEM = (
    "You are a YouTube SEO expert. Always respond with strict JSON. No commentary."
)


async def generate_metadata(project: dict, script: dict, scenes: list = None) -> dict:
    if isinstance(script, str):
        script = {"full_script": script}
    topic = project.get("topic", project.get("title", "business story"))
    hook = script.get("selected_hook", topic)

    user = f"""Generate YouTube metadata JSON:
{{
  "title_options": ["title1", "title2", "title3"],
  "selected_title": "best title",
  "description": "150-200 word description with keywords",
  "tags": ["tag1", "tag2", ...],
  "hashtags": ["#hashtag1", "#hashtag2", ...],
  "chapters": [{{"time": "0:00", "label": "Intro"}}, ...],
  "pinned_comment": "engaging pinned comment to drive discussion"
}}

Topic: {topic}
Hook: {hook}

Write specifically about this Topic. Do NOT use a generic entrepreneur/startup origin-story framing (billion dollar empire, instant noodles, investors said no) unless the Topic explicitly asks for it.
Do not include BEAT markers, section headers, or structural labels in any output text."""

    result = await _llm_json(META_SYSTEM, user, f"meta-{project.get('id', uuid.uuid4())}")
    if result:
        return result

    short_topic = truncate_words(topic, 60)
    return {
        "title_options": [
            f"{short_topic} — Explained",
            f"The Truth About {short_topic}",
            f"{short_topic}: What You Need to Know",
        ],
        "selected_title": f"{short_topic} — Explained",
        "description": (
            f"This video breaks down {short_topic}. "
            "We cover what really drives it, the mistakes most people make, "
            "and the practical takeaways you can apply right away. "
            "Subscribe for new videos every week."
        ),
        "tags": [short_topic, project.get("niche", "education"), "explained", "guide", "how it works"],
        "hashtags": ["#explained", "#education", "#howto"],
        "chapters": [
            {"time": "0:00", "label": "Introduction"},
            {"time": "1:00", "label": "The Fundamentals"},
            {"time": "2:30", "label": "Key Insights"},
            {"time": "4:00", "label": "Practical Takeaways"},
            {"time": "5:00", "label": "Wrap-Up"},
        ],
        "pinned_comment": f"What surprised you most about {short_topic}? Comment below 👇",
    }


# ------------------------------ THUMBNAIL CONCEPTS ------------------------------

THUMB_SYSTEM = (
    "You are a YouTube thumbnail designer. Always respond with strict JSON, no commentary. "
    "Each concept must include: thumbnail_title_text, visual_composition, emotion_angle, "
    "background_idea, subject_focal_point, colour_direction, click_trigger, image_prompt."
)


def _coerce_thumbnail_concepts(concepts: list) -> list[dict]:
    """Normalise thumbnail concepts to the schema expected by models + frontend."""
    keys = [
        "thumbnail_title_text", "visual_composition", "emotion_angle",
        "background_idea", "subject_focal_point", "colour_direction",
        "click_trigger", "image_prompt",
    ]
    out = []
    for c in concepts:
        if not isinstance(c, dict):
            continue
        out.append({k: str(c.get(k) or "").strip() for k in keys})
    return out


async def generate_thumbnail_concepts(project: dict, script: dict) -> list[dict]:
    topic = project.get("topic", project.get("title", "business story"))
    niche = project.get("niche", "business")

    user = f"""Generate 3 YouTube thumbnail concepts as JSON:
{{"concepts": [
  {{
    "thumbnail_title_text": "bold 3-5 word title",
    "visual_composition": "description of layout and framing",
    "emotion_angle": "curiosity|shock|inspiration|fear",
    "background_idea": "description of background / setting",
    "subject_focal_point": "the single dominant subject",
    "colour_direction": "primary and accent colors",
    "click_trigger": "why a viewer would click",
    "image_prompt": "detailed image-generation prompt, no text baked in, 16:9"
  }}
]}}

Topic: {topic}
Niche: {niche}"""

    result = await _llm_json(THUMB_SYSTEM, user, f"thumb-{project.get('id', uuid.uuid4())}")
    if result and isinstance(result.get("concepts"), list):
        coerced = _coerce_thumbnail_concepts(result["concepts"])
        if coerced:
            return coerced

    # Topic-aware deterministic fallback — word-boundary cut, no clickbait
    # auto-suffix (the old "… EXPOSED" append truncated titles mid-word).
    short_topic = truncate_words(topic, 40)
    return [
        {
            "thumbnail_title_text": short_topic.upper(),
            "visual_composition": "Dark background with bold centered text and single focal graphic",
            "emotion_angle": "curiosity",
            "background_idea": "Textured black background with subtle gradient",
            "subject_focal_point": f"Symbol or imagery representing {short_topic}",
            "colour_direction": "Black background, yellow text, green accents",
            "click_trigger": "Contradiction — implies common belief is wrong",
            "image_prompt": (
                f"YouTube thumbnail, 16:9, ultra-sharp, high contrast, dark textured background, "
                f"large bold yellow headline text, green accent graphic related to {short_topic}, "
                "cinematic lighting, no baked text"
            ),
        },
        {
            "thumbnail_title_text": f"THE {short_topic.upper()} TRUTH",
            "visual_composition": "Split screen: before vs after / problem vs solution",
            "emotion_angle": "shock",
            "background_idea": "Red and black high-contrast split background",
            "subject_focal_point": f"Two contrasting visuals of {short_topic}",
            "colour_direction": "Red and black, high contrast",
            "click_trigger": "Transformation and revelation",
            "image_prompt": (
                f"YouTube thumbnail, 16:9, dramatic split-screen composition, dark red and black, "
                f"contrasting visuals representing {short_topic}, cinematic lighting, no baked text"
            ),
        },
        {
            "thumbnail_title_text": f"{short_topic.upper()} SECRETS",
            "visual_composition": "Documentary style, serious tone, single focal subject",
            "emotion_angle": "intrigue",
            "background_idea": "Dark teal textured background with subtle film grain",
            "subject_focal_point": f"Mysterious central subject related to {short_topic}",
            "colour_direction": "Dark teal and gold",
            "click_trigger": "Hidden story promise",
            "image_prompt": (
                "YouTube thumbnail, 16:9, documentary style, dark teal background, gold accents, "
                "mysterious central subject, film grain, cinematic lighting, no baked text"
            ),
        },
    ]

# Alias for backwards compat - routes.py expects generate_thumbnails
async def generate_thumbnails(project: dict, script: dict = None):
    if script is None:
        script = project.get("full_script", {}) if isinstance(project, dict) else {}
        if isinstance(script, str):
            script = {"full_script": script}
    return await generate_thumbnail_concepts(project, script)
