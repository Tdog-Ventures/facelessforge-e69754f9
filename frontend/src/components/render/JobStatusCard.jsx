import React from "react";
import {
  Loader2, AlertCircle, CheckCircle2, Download, RefreshCw, Square as StopIcon,
} from "lucide-react";

const ACTIVE_STATES = new Set(["queued", "validating", "preparing_assets", "rendering"]);

function fmtBytes(n) {
  if (!n && n !== 0) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}
function fmtSec(s) {
  if (!s && s !== 0) return "—";
  const m = Math.floor(s / 60);
  const r = Math.round(s % 60);
  return m > 0 ? `${m}m ${String(r).padStart(2, "0")}s` : `${r}s`;
}

export default function JobStatusCard({
  job, project, selectedThumbnail, canEdit, cancelling, starting,
  onCancel, onRetry,
}) {
  const failed = job.status === "failed";
  const completed = job.status === "completed";
  const inProgress = ACTIVE_STATES.has(job.status);

  return (
    <div
      data-testid={`render-job-${job.status}`}
      className="border p-5 rounded-sm space-y-3"
      style={{
        borderColor: failed ? "#FF336666"
                     : completed ? "#00FF6666"
                     : job.status === "cancelled" ? "#52525B"
                     : "#00E5FF66",
        background: failed ? "rgba(255,51,102,0.05)"
                     : completed ? "rgba(0,255,102,0.04)"
                     : "rgba(0,229,255,0.04)",
      }}
    >
      <div className="flex items-start gap-3">
        {failed ? <AlertCircle size={18} className="text-[#FF3366] mt-0.5" />
          : completed ? <CheckCircle2 size={18} className="text-[#00FF66] mt-0.5" />
          : job.status === "cancelled" ? <StopIcon size={18} className="text-zinc-500 mt-0.5" />
          : <Loader2 size={18} className="text-[#00E5FF] mt-0.5 animate-spin" />}
        <div className="flex-1 min-w-0">
          <div className="font-mono text-[11px] uppercase tracking-widest mb-1"
               style={{ color: failed ? "#FF3366" : completed ? "#00FF66" : "#00E5FF" }}>
            Render · {job.status}
          </div>
          <div className="text-sm text-zinc-300">
            Progress {job.progress}% · step{" "}
            <span className="font-mono text-[#00E5FF]">{job.current_step}</span>
          </div>
          {job.error_message && (
            <div data-testid="render-error" className="mt-2 text-sm text-[#FF3366] leading-relaxed">
              {job.error_message}
            </div>
          )}
        </div>
        {inProgress && canEdit && (
          <button
            data-testid="render-cancel-btn"
            onClick={onCancel}
            disabled={cancelling}
            className="flex items-center gap-1 font-mono text-[10px] uppercase tracking-widest border border-zinc-700 text-zinc-300 hover:text-[#FF3366] hover:border-[#FF3366] px-3 py-1.5 rounded-sm transition-colors disabled:opacity-50"
          >
            {cancelling ? <Loader2 size={11} className="animate-spin" /> : <StopIcon size={11} />}
            Cancel
          </button>
        )}
      </div>
      <div className="w-full h-1 bg-[#27272A] rounded-sm overflow-hidden">
        <div
          className="h-full transition-all duration-300"
          style={{
            width: `${job.progress || 0}%`,
            background: failed ? "#FF3366" : completed ? "#00FF66" : "#00E5FF",
            boxShadow: completed ? "0 0 12px #00FF66" : "0 0 8px #00E5FF",
          }}
        />
      </div>

      {completed && job.output_url && (
        <div data-testid="render-output" className="space-y-3 pt-2">
          <video
            data-testid="render-video-player"
            controls
            src={job.output_url}
            preload="metadata"
            poster={selectedThumbnail?.preview_url}
            className="w-full rounded-sm border border-zinc-800 bg-black aspect-video"
          />
          <div className="flex flex-wrap items-center gap-2 font-mono text-[10px] text-zinc-500 uppercase tracking-widest">
            <span>1920×1080</span>
            <span>· 30 fps</span>
            <span>· H.264 / AAC</span>
            <span>· {fmtSec(job.duration)}</span>
            <span>· {fmtBytes(job.file_size)}</span>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <a
              data-testid="render-download-btn"
              href={job.output_url}
              download={`${(project.name || "facelessforge").replace(/[^a-z0-9_-]/gi, "_")}.mp4`}
              className="inline-flex items-center gap-2 bg-[#00FF66] text-black font-semibold text-sm px-4 py-2 rounded-sm hover:bg-[#33FF80] transition-colors"
            >
              <Download size={14} /> Download MP4
            </a>
            {canEdit && (
              <button
                data-testid="render-retry-btn"
                onClick={onRetry}
                disabled={starting}
                className="inline-flex items-center gap-2 border border-zinc-700 text-zinc-300 hover:text-[#00E5FF] hover:border-[#00E5FF] font-mono text-[11px] uppercase tracking-widest px-3 py-2 rounded-sm transition-colors"
              >
                {starting ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />}
                Render again
              </button>
            )}
          </div>
        </div>
      )}

      {failed && canEdit && (
        <button
          data-testid="render-retry-btn"
          onClick={onRetry}
          disabled={starting}
          className="inline-flex items-center gap-2 border border-zinc-700 text-zinc-300 hover:text-[#00E5FF] hover:border-[#00E5FF] font-mono text-[11px] uppercase tracking-widest px-3 py-2 rounded-sm transition-colors"
        >
          {starting ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />}
          Retry render
        </button>
      )}
    </div>
  );
}
