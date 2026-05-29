import React, { useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { Play, Loader2, Film } from "lucide-react";
import { api, formatApiError, API } from "../lib/api";
import PrereqChecklist from "./render/PrereqChecklist";
import RenderPreviewCards from "./render/RenderPreviewCards";
import JobStatusCard from "./render/JobStatusCard";
import JobHistory from "./render/JobHistory";

const ACTIVE_STATES = new Set(["queued", "validating", "preparing_assets", "rendering"]);
const TERMINAL = new Set(["completed", "failed", "cancelled"]);

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
      } catch {/* silent retry */}
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
  const inProgress = activeJob && ACTIVE_STATES.has(activeJob.status);

  const selectedThumbnail = (assets || []).find(
    (a) => a.id === project.selected_thumbnail_asset_id && a.asset_type === "generated_thumbnail"
  );
  const selectedVoice = (assets || []).find(
    (a) => a.id === project.selected_voiceover_asset_id && a.asset_type === "voiceover_audio"
  );

  return (
    <div className="space-y-6">
      <PrereqChecklist preflight={preflight} onRefresh={refreshAll} />

      <RenderPreviewCards
        selectedThumbnail={selectedThumbnail}
        selectedVoice={selectedVoice}
      />

      {activeJob && (
        <JobStatusCard
          job={activeJob}
          project={project}
          selectedThumbnail={selectedThumbnail}
          canEdit={canEdit}
          cancelling={cancelling}
          starting={starting}
          onCancel={cancel}
          onRetry={start}
        />
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

      <JobHistory jobs={jobs} />
    </div>
  );
}
