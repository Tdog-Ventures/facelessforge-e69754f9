import { formatApiError } from "./api";

export const MIN_MANUAL_WORDS = 20;
export const MAX_SCRIPT_CHARS = 20000;
export const WORDS_PER_SECOND = 2.5;

export function countWords(text) {
  const matches = String(text || "").match(/\b\w+\b/g);
  return matches ? matches.length : 0;
}

export function estimateDurationSeconds(words) {
  return Math.floor(Number(words || 0) / WORDS_PER_SECOND);
}

export function formatNarration(totalSeconds) {
  const s = Math.max(0, Math.floor(Number(totalSeconds || 0)));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

export function validateManualScript(text) {
  const cleaned = String(text || "").replace(/\x00/g, "").trim();
  const words = countWords(cleaned);
  if (!cleaned) {
    return { ok: false, error: "Write or paste a script first.", words: 0, seconds: 0 };
  }
  if (cleaned.length > MAX_SCRIPT_CHARS) {
    return { ok: false, error: `Script exceeds ${MAX_SCRIPT_CHARS} characters.`, words, seconds: estimateDurationSeconds(words) };
  }
  if (words < MIN_MANUAL_WORDS) {
    return { ok: false, error: `Script needs at least ${MIN_MANUAL_WORDS} words (currently ${words}).`, words, seconds: estimateDurationSeconds(words) };
  }
  return { ok: true, error: null, words, seconds: estimateDurationSeconds(words), cleaned };
}

export function invalidationNotice(data) {
  const inv = data?.script_invalidated;
  if (!inv) return null;
  const parts = [];
  if (inv.scenes) parts.push(`${inv.scenes} scene(s)`);
  if (inv.metadata) parts.push("metadata");
  if (inv.assets) parts.push(`${inv.assets} asset(s)`);
  if (!parts.length) return null;
  return data?.script_notice || `Script updated — regenerate: ${parts.join(", ")}. Completed videos were kept.`;
}

export async function saveScript(api, projectId, { full_script, source }) {
  try {
    const { data } = await api.patch(`/projects/${projectId}/script`, { full_script, source });
    return data;
  } catch (err) {
    throw new Error(formatApiError(err.response?.data?.detail) || err.message || "Save failed");
  }
}
