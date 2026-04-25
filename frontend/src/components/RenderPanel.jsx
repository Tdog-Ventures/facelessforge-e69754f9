import React, { useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import {
  Play, Loader2, AlertCircle, CheckCircle2, Download, RefreshCw,
  Square as StopIcon, Film,
} from "lucide-react";
import { api, formatApiError, API } from "../lib/api";

const ACTIVE_STATES = new Set(["queued", "validating", "preparing_assets", "rendering"]);
const TERMINAL = new Set(["completed", "failed", "cancelled"]);

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

export default function RenderPanel({
  projectId, project, script, scenes, metadata, assets, canEdit, onChange,
}) {
  const [preflight, setPreflight] = useState(null);
  const [jobs, setJobs] = useState([]);
  const [activeJob, setActiveJob] = useState(null);
  const [starting, setStarting] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const pollRef = useRef(null);

  const refreshAll = async () => {
    try {
      const [pf, jl] = await Promise.all([
        api.get(`/projects/${projectId}/render/preflight`),
        api.get(`/projects/${projectId}/render/jobs`),
      ]);
      setPreflight(pf.data);
      setJobs(jl.data);
      const active = jl.data.find((j) => ACTIVE_STATES.has(j.status));
      const latest = jl.data[0] || null;
      setActiveJob(active || latest);
    } catch (err) {
      toast.error("Failed to load render data", {
        description: formatApiError(err.response?.data?.detail) || err.message,
      });
    }
  };

  useEffect(() => {
    refreshAll();
    return () => clearInterval(pollRef.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  // Poll while a job is active
  useEffect(() => {
    clearInterval(pollRef.current);
    if (!activeJob || !ACTIVE_STATES.has(activeJob.status)) return;
    pollRef.current = setInterval(async () => {
      try {
        const { data } = await api.get(`/projects/${projectId}/render/jobs/${activeJob.id}`);
        setActiveJob(data);
        if (TERMINAL.has(data.status)) {
          clearInterval(pollRef.current);
          // Refresh project view + jobs
          const [pf, jl, full] = await Promise.all([
            api.get(`/projects/${projectId}/render/preflight`),
            api.get(`/projects/${projectId}/render/jobs`),
            api.get(`/projects/${projectId}`),
          ]);
          setPreflight(pf.data);
          setJobs(jl.data);
          onChange(full.data);
          if (data.status === "completed") {
            toast.success("Render complete", { description: "MP4 ready below." });
          } else if (data.status === "failed") {
            toast.error("Render failed", { description: data.error_message || "Unknown error" });
          } else if (data.status === "cancelled") {
            toast("Render cancelled");
          }
        }
      } catch {
        // silent retry
      }
    }, 2500);
    return () => clearInterval(pollRef.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeJob?.id, activeJob?.status]);

  const start = async () => {
    setStarting(true);
    try {
      const { data } = await api.post(`/projects/${projectId}/render/start`, {});
      setActiveJob(data);
      toast.success("Render queued");
      const jl = await api.get(`/projects/${projectId}/render/jobs`);
      setJobs(jl.data);
    } catch (err) {
      const detail = err.response?.data?.detail;
      if (typeof detail === "object" && detail?.issues) {
        toast.error("Cannot start render", { description: detail.issues.join(" · ") });
      } else if (err.response?.status === 409) {
        toast.error("Render already running");
      } else {
        toast.error("Start failed", { description: formatApiError(detail) || err.message });
      }
    } finally {
      setStarting(false);
    }
  };

  const cancel = async () => {
    if (!activeJob) return;
    setCancelling(true);
    try {
      await api.post(`/projects/${projectId}/render/jobs/${activeJob.id}/cancel`);
      toast("Cancelling…");
    } catch (err) {
      toast.error("Cancel failed", { description: formatApiError(err.response?.data?.detail) || err.message });
    } finally {
      setCancelling(false);
    }
  };

  if (!preflight) {
    return (
      <div className="p-8 text-sm text-zinc-500 font-mono">Loading render state…</div>
    );
  }

  const canRender = preflight.ok && !ACTIVE_STATES.has(activeJob?.status || "");
  const completed = activeJob?.status === "completed";
  const failed = activeJob?.status === "failed";
  const inProgress = activeJob && ACTIVE_STATES.has(activeJob.status);

  // Selected thumb / voice for preview
  const selectedThumbnail = (assets || []).find(
    (a) => a.id === project.selected_thumbnail_asset_id && a.asset_type === "generated_thumbnail"
  );
  const selectedVoice = (assets || []).find(
    (a) => a.id === project.selected_voiceover_asset_id && a.asset_type === "voiceover_audio"
  );

  return (
    <div className="space-y-6">
      {/* Prerequisite checklist */}
      <div data-testid="render-checklist" className="border border-zinc-800 bg-[#121212] p-6 rounded-sm">
        <div className="flex items-center justify-between mb-4">
          <div className="font-mono text-[11px] uppercase tracking-widest text-zinc-500">
            Prerequisites
          </div>
          <button
            data-testid="render-refresh"
            onClick={refreshAll}
            className="flex items-center gap-1 font-mono text-[10px] uppercase tracking-widest text-zinc-400 hover:text-[#00E5FF]"
          >
            <RefreshCw size={11} /> Refresh
          </button>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
          {preflight.checklist.map((c) => (
            <div
              key={c.key}
              data-testid={`pre-${c.key}`}
              className={`flex items-start gap-3 p-3 border rounded-sm ${
                c.ok ? "border-[#00FF66]/25 bg-[#00FF66]/5"
                     : "border-[#FF3366]/30 bg-[#FF3366]/5"
              }`}
            >
              {c.ok ? (
                <CheckCircle2 size={16} className="text-[#00FF66] mt-0.5" />
              ) : (
                <AlertCircle size={16} className="text-[#FF3366] mt-0.5" />
              )}
              <div className="flex-1 min-w-0">
                <div className="text-sm text-white">{c.label}</div>
                <div className="font-mono text-[10px] text-zinc-500 mt-1">{c.hint}</div>
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* Preview cards */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div className="border border-zinc-800 bg-[#121212] rounded-sm overflow-hidden">
          <div className="px-4 py-3 border-b border-zinc-800 font-mono text-[10px] uppercase tracking-widest text-zinc-500">
            Intro · selected thumbnail
          </div>
          {selectedThumbnail ? (
            <img
              src={selectedThumbnail.preview_url}
              alt="thumbnail"
              data-testid="render-preview-thumbnail"
              className="w-full aspect-video object-cover"
            />
          ) : (
            <div className="aspect-video flex items-center justify-center text-zinc-500 font-mono text-xs">
              No selected thumbnail
            </div>
          )}
        </div>
        <div className="border border-zinc-800 bg-[#121212] rounded-sm overflow-hidden">
          <div className="px-4 py-3 border-b border-zinc-800 font-mono text-[10px] uppercase tracking-widest text-zinc-500">
            Audio bed · voiceover
          </div>
          {selectedVoice ? (
            <div className="p-4 space-y-3">
              <div className="flex items-center gap-2">
                <span className="font-mono text-[10px] uppercase tracking-widest text-[#FF66CC]">
                  {selectedVoice.voice_style}
                </span>
                <span className="font-mono text-[10px] text-zinc-500">
                  {fmtSec(selectedVoice.duration)}
                </span>
              </div>
              <audio
                data-testid="render-preview-voiceover"
                controls
                src={selectedVoice.preview_url}
                preload="metadata"
                className="w-full h-10"
              />
            </div>
          ) : (
            <div className="aspect-video flex items-center justify-center text-zinc-500 font-mono text-xs">
              No selected full-script voiceover (will use scene-level audio if available)
            </div>
          )}
        </div>
      </div>

      {/* Active / latest job status */}
      {activeJob && (
        <div
          data-testid={`render-job-${activeJob.status}`}
          className="border p-5 rounded-sm space-y-3"
          style={{
            borderColor: failed ? "#FF336666"
                         : completed ? "#00FF6666"
                         : activeJob.status === "cancelled" ? "#52525B"
                         : "#00E5FF66",
            background: failed ? "rgba(255,51,102,0.05)"
                         : completed ? "rgba(0,255,102,0.04)"
                         : "rgba(0,229,255,0.04)",
          }}
        >
          <div className="flex items-start gap-3">
            {failed ? <AlertCircle size={18} className="text-[#FF3366] mt-0.5" />
              : completed ? <CheckCircle2 size={18} className="text-[#00FF66] mt-0.5" />
              : activeJob.status === "cancelled" ? <StopIcon size={18} className="text-zinc-500 mt-0.5" />
              : <Loader2 size={18} className="text-[#00E5FF] mt-0.5 animate-spin" />}
            <div className="flex-1 min-w-0">
              <div className="font-mono text-[11px] uppercase tracking-widest mb-1"
                   style={{ color: failed ? "#FF3366" : completed ? "#00FF66" : "#00E5FF" }}>
                Render · {activeJob.status}
              </div>
              <div className="text-sm text-zinc-300">
                Progress {activeJob.progress}% · step{" "}
                <span className="font-mono text-[#00E5FF]">{activeJob.current_step}</span>
              </div>
              {activeJob.error_message && (
                <div data-testid="render-error" className="mt-2 text-sm text-[#FF3366] leading-relaxed">
                  {activeJob.error_message}
                </div>
              )}
            </div>
            {inProgress && canEdit && (
              <button
                data-testid="render-cancel-btn"
                onClick={cancel}
                disabled={cancelling}
                className="flex items-center gap-1 font-mono text-[10px] uppercase tracking-widest border border-zinc-700 text-zinc-300 hover:text-[#FF3366] hover:border-[#FF3366] px-3 py-1.5 rounded-sm transition-colors disabled:opacity-50"
              >
                {cancelling ? <Loader2 size={11} className="animate-spin" /> : <StopIcon size={11} />}
                Cancel
              </button>
            )}
          </div>
          {/* Progress bar */}
          <div className="w-full h-1 bg-[#27272A] rounded-sm overflow-hidden">
            <div
              className="h-full transition-all duration-300"
              style={{
                width: `${activeJob.progress || 0}%`,
                background: failed ? "#FF3366" : completed ? "#00FF66" : "#00E5FF",
                boxShadow: completed ? "0 0 12px #00FF66" : "0 0 8px #00E5FF",
              }}
            />
          </div>

          {completed && activeJob.output_url && (
            <div data-testid="render-output" className="space-y-3 pt-2">
              <video
                data-testid="render-video-player"
                controls
                src={activeJob.output_url}
                preload="metadata"
                poster={selectedThumbnail?.preview_url}
                className="w-full rounded-sm border border-zinc-800 bg-black aspect-video"
              />
              <div className="flex flex-wrap items-center gap-2 font-mono text-[10px] text-zinc-500 uppercase tracking-widest">
                <span>1920×1080</span>
                <span>· 30 fps</span>
                <span>· H.264 / AAC</span>
                <span>· {fmtSec(activeJob.duration)}</span>
                <span>· {fmtBytes(activeJob.file_size)}</span>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <a
                  data-testid="render-download-btn"
                  href={activeJob.output_url}
                  download={`${(project.name || "facelessforge").replace(/[^a-z0-9_-]/gi, "_")}.mp4`}
                  className="inline-flex items-center gap-2 bg-[#00FF66] text-black font-semibold text-sm px-4 py-2 rounded-sm hover:bg-[#33FF80] transition-colors"
                >
                  <Download size={14} /> Download MP4
                </a>
                {canEdit && (
                  <button
                    data-testid="render-retry-btn"
                    onClick={start}
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
              onClick={start}
              disabled={starting}
              className="inline-flex items-center gap-2 border border-zinc-700 text-zinc-300 hover:text-[#00E5FF] hover:border-[#00E5FF] font-mono text-[11px] uppercase tracking-widest px-3 py-2 rounded-sm transition-colors"
            >
              {starting ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />}
              Retry render
            </button>
          )}
        </div>
      )}

      {/* Action bar */}
      <div className="flex flex-wrap items-center gap-3">
        {canEdit && (
          <button
            data-testid="render-start-btn"
            onClick={start}
            disabled={!canRender || starting}
            title={!preflight.ok ? `Missing: ${preflight.issues.join(", ")}` : ""}
            className="flex items-center gap-2 bg-[#00E5FF] text-black font-semibold text-sm px-5 py-2.5 rounded-sm hover:bg-[#33EFFF] disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            {starting ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} />}
            {inProgress ? "Render running…" : completed ? "Render again" : "Start render"}
          </button>
        )}
        <a
          data-testid="export-package-zip"
          href={`${API}/projects/${projectId}/export/package.zip`}
          className="flex items-center gap-2 border border-zinc-800 text-white text-sm px-5 py-2.5 rounded-sm hover:border-[#00E5FF] hover:text-[#00E5FF] transition-colors"
        >
          <Film size={14} /> Export package (ZIP)
        </a>
      </div>

      {/* Job history */}
      {jobs.length > 1 && (
        <div className="border border-zinc-800 bg-[#121212] rounded-sm overflow-hidden">
          <div className="px-4 py-3 border-b border-zinc-800 font-mono text-[10px] uppercase tracking-widest text-zinc-500">
            Render history · {jobs.length} jobs
          </div>
          <div className="divide-y divide-zinc-800">
            {jobs.slice(0, 8).map((j) => (
              <div
                key={j.id}
                data-testid={`render-history-${j.id}`}
                className="px-4 py-3 flex items-center gap-3 text-sm"
              >
                <span className="font-mono text-[10px] uppercase tracking-widest"
                      style={{
                        color: j.status === "completed" ? "#00FF66"
                          : j.status === "failed" ? "#FF3366"
                          : j.status === "cancelled" ? "#71717A" : "#00E5FF",
                      }}>
                  {j.status}
                </span>
                <span className="font-mono text-[10px] text-zinc-500">
                  {new Date(j.created_at).toLocaleString()}
                </span>
                <span className="flex-1" />
                {j.output_url && (
                  <a
                    href={j.output_url}
                    target="_blank"
                    rel="noreferrer"
                    className="font-mono text-[10px] uppercase tracking-widest text-[#00E5FF] hover:underline"
                  >
                    Open
                  </a>
                )}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
