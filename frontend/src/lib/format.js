// Status metadata and helpers
export const STATUS_META = {
  DRAFT:               { label: "Draft",              color: "#A1A1AA", bg: "rgba(161,161,170,0.08)" },
  SCRIPT_GENERATED:    { label: "Script Ready",       color: "#00E5FF", bg: "rgba(0,229,255,0.08)" },
  SCENES_GENERATED:    { label: "Scenes Ready",       color: "#00E5FF", bg: "rgba(0,229,255,0.08)" },
  METADATA_GENERATED:  { label: "Metadata Ready",     color: "#7B61FF", bg: "rgba(123,97,255,0.1)" },
  ASSETS_READY:        { label: "Assets Ready",       color: "#7B61FF", bg: "rgba(123,97,255,0.1)" },
  READY_TO_RENDER:     { label: "Ready to Render",    color: "#FFB020", bg: "rgba(255,176,32,0.1)" },
  COMPLETED:           { label: "Completed",          color: "#00FF66", bg: "rgba(0,255,102,0.1)" },
  FAILED:              { label: "Failed",             color: "#FF3366", bg: "rgba(255,51,102,0.1)" },
};

export function qualityColor(score) {
  if (score >= 90) return "#00FF66";
  if (score >= 70) return "#00E5FF";
  if (score >= 40) return "#FFB020";
  return "#FF3366";
}

export function qualityLabel(score) {
  if (score >= 90) return "Publish Ready";
  if (score >= 70) return "Good";
  if (score >= 40) return "Needs Work";
  return "Poor";
}

export function formatDuration(seconds) {
  const s = Math.max(0, Math.round(seconds));
  const m = Math.floor(s / 60);
  const r = s % 60;
  return `${m}:${String(r).padStart(2, "0")}`;
}

export function sceneBudgetLabel({ targetDuration = 0, productionMode, scenes = [] }) {
  const target = Number(targetDuration) || 0;
  const intro = productionMode === "commercial_ad" ? 0 : 2.5;
  const narrationBudget = Math.max(0, target - intro);
  const expected = Math.max(1, Math.ceil(narrationBudget / 30));
  const actual = scenes.length;
  const summed = scenes.reduce((sum, scene) => sum + (Number(scene.duration) || 0), 0);
  const lastEnd = actual ? Number(scenes[actual - 1].end_time) || 0 : 0;
  const mismatch = actual > 0 && actual !== expected;
  const text = `target ${target}s · narration budget ${narrationBudget}s · expected ${expected} scenes · stored ${actual} · timeline ends ${formatDuration(lastEnd)} · summed durations ${Math.round(summed)}s`;
  return { text, mismatch, expected, actual, narrationBudget };
}

export function formatCurrency(n) {
  return `$${Number(n || 0).toFixed(2)}`;
}

export function relativeTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}
