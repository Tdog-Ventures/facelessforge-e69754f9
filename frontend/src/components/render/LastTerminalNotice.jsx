import React from "react";
import { AlertCircle, Square as StopIcon } from "lucide-react";

function timeAgo(iso) {
  if (!iso) return "";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "";
  const diff = Math.max(0, Date.now() - t);
  const m = Math.floor(diff / 60000);
  if (m < 1) return "just now";
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  return `${d}d ago`;
}

/**
 * Small, calm notice shown above the action bar when the most recent render
 * job terminated as cancelled or failed. Replaces the frozen progress card
 * so the user is never confused into thinking the render is still running.
 */
export default function LastTerminalNotice({ job }) {
  if (!job) return null;
  if (job.status !== "cancelled" && job.status !== "failed") return null;

  const failed = job.status === "failed";
  const Icon = failed ? AlertCircle : StopIcon;
  const accent = failed ? "#FF3366" : "#71717A";
  const label = failed ? "Last render failed" : "Last render was cancelled";

  return (
    <div
      data-testid={`render-last-${job.status}`}
      className="flex items-start gap-3 border rounded-sm px-4 py-3"
      style={{
        borderColor: `${accent}55`,
        background: failed ? "rgba(255,51,102,0.04)" : "rgba(113,113,122,0.05)",
      }}
    >
      <Icon size={16} style={{ color: accent }} className="mt-0.5 shrink-0" />
      <div className="flex-1 min-w-0">
        <div
          className="font-mono text-[10px] uppercase tracking-widest"
          style={{ color: accent }}
        >
          {label} · {timeAgo(job.completed_at || job.updated_at || job.created_at)}
        </div>
        {failed && job.error_message ? (
          <div className="text-sm text-zinc-400 mt-1 leading-relaxed line-clamp-2">
            {job.error_message}
          </div>
        ) : (
          <div className="text-sm text-zinc-400 mt-1">
            Start a fresh render below — your project is still ready.
          </div>
        )}
      </div>
    </div>
  );
}
