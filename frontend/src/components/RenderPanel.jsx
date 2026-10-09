import React, { useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { Play, Loader2, Film } from "lucide-react";
import { api, formatApiError, API } from "../lib/api";
import PrereqChecklist from "./render/PrereqChecklist";
import RenderPreviewCards from "./render/RenderPreviewCards";
import JobStatusCard from "./render/JobStatusCard";
import LastTerminalNotice from "./render/LastTerminalNotice";
import JobHistory from "./render/JobHistory";
import VoiceStylePicker from "./voiceover/VoiceStylePicker";

const ACTIVE_STATES = new Set(["queued", "validating", "preparing_assets", "rendering"]);
const TERMINAL = new Set(["completed", "failed", "cancelled"]);
// Only completed jobs deserve the full status card (they show the player +
// download). In-progress jobs also show it. Cancelled / failed get a calm
// banner instead (see LastTerminalNotice) and live in the history list.
const PROMOTABLE = new Set([...ACTIVE_STATES, "completed"]);

export default function RenderPanel({
  projectId, project, script, scenes, metadata, assets, canEdit, onChange,
}) {
  const [preflight, setPreflight] = useState(null);
  const [jobs, setJobs] = useState([]);
  const [activeJob, setActiveJob] = useState(null);
  const [starting, setStarting] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [voiceStyle, setVoiceStyle] = useState(project?.voice_style || project?.voice_id || "narrator");
  const [burnSubtitles, setBurnSubtitles] = useState(false);
  // was: aspect_ratio: "9:16"
  const [aspect, setAspect] = useState(project?.aspect_ratio || "9:16");
  const pollRef = useRef(null);

  useEffect(() => {
    if (project?.voice_style || project?.voice_id) setVoiceStyle(project.voice_style || project.voice_id);
    if (project?.aspect_ratio) setAspect(project.aspect_ratio);
    if (typeof project?.burn_subtitles === "boolean") setBurnSubtitles(project.burn_subtitles);
  }, [project?.voice_style, project?.voice_id, project?.aspect_ratio, project?.burn_subtitles]);

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
      // Only show the in-detail JobStatusCard for active or completed jobs.
      // Cancelled / failed jobs surface as a calm banner via LastTerminalNotice
      // and remain accessible in the JobHistory list below.
      setActiveJob(active || (latest && PROMOTABLE.has(latest.status) ? latest : null));
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
        if (TERMINAL.has(data.status)) {
          clearInterval(pollRef.current);
          // Only keep the active card for completed (player + download).
          // Cancelled/failed drop out so LastTerminalNotice + history take over.
          setActiveJob(PROMOTABLE.has(data.status) ? data : null);
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
        } else {
          setActiveJob(data);
        }
      } catch {/* silent retry */}
    }, 2500);
    return () => clearInterval(pollRef.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeJob?.id, activeJob?.status]);

  const getAudioDuration = (url) => new Promise((resolve) => {
    if (!url) return resolve(0);
    const audio = new Audio();
    audio.preload = "metadata";
    audio.onloadedmetadata = () => resolve(audio.duration || 0);
    audio.onerror = () => resolve(0);
    audio.src = url;
    setTimeout(() => resolve(0), 5000);
  });

  const start = async () => {
    setStarting(true);
    // Pre-render validation: VO vs scenes total mismatch
    try {
      const voAssets = (assets || []).filter(a => a.asset_type === "voiceover_audio" && a.status !== "rejected");
      const fullVO = voAssets.find(a => !a.scene_id && a.id === project.selected_voiceover_asset_id);
      let voDuration = 0;
      if (fullVO?.duration) voDuration = parseFloat(fullVO.duration);
      else if (voAssets.filter(a => a.scene_id).length > 0) {
        const byScene = {};
        voAssets.filter(a => a.scene_id).forEach(v => {
          if (!byScene[v.scene_id] || String(v.created_at) > String(byScene[v.scene_id].created_at)) byScene[v.scene_id] = v;
        });
        voDuration = Object.values(byScene).reduce((sum, v) => sum + parseFloat(v.duration || 0), 0);
        // If duration not in asset, try to probe via audio element
        if (!voDuration) {
          for (const v of Object.values(byScene)) {
            // eslint-disable-next-line no-await-in-loop
            const d = await getAudioDuration(v.preview_url || v.url);
            voDuration += d;
          }
        }
      } else if (fullVO?.preview_url) {
        voDuration = await getAudioDuration(fullVO.preview_url);
      }
      const scenesTotal = (scenes || []).reduce((sum, s) => sum + parseFloat(s.duration || 30), 0);
      const target = parseInt(project?.target_duration || 0, 10);
      const mismatch = Math.abs(voDuration - scenesTotal);
      if (voDuration && scenesTotal && mismatch > 5) {
        // VO is master: backend _build_subclip_plan() scales video to VO
        // exactly (render.py). Warn only — do not block. Video will match VO.
        toast.warning("Duration auto-alignment: video will match VO", {
          description: `Target ${target}s, VO ${voDuration.toFixed(1)}s, Scenes ${scenesTotal.toFixed(1)}s — Δ ${mismatch.toFixed(1)}s. VO is master, video will be scaled to ${voDuration.toFixed(1)}s.`,
          duration: 8000,
        });
        console.warn("[Render] VO vs Scenes mismatch — auto-aligning to VO", { target, voDuration, scenesTotal, mismatch, projectId });
      }
    } catch (e) {
      console.warn("[Render] Preflight validation failed", e);
    }
    try {
      const { data } = await api.post(`/projects/${projectId}/render/start`, { voice_style: voiceStyle, voice_id: voiceStyle, burn_subtitles: burnSubtitles, aspect_ratio: aspect });
      setActiveJob(data);
      toast.success("Render queued");
      const jl = await api.get(`/projects/${projectId}/render/jobs`);
      setJobs(jl.data);
      console.log("[Render] Queued", data);
    } catch (err) {
      const detail = err.response?.data?.detail;
      const msg = typeof detail === "object" ? JSON.stringify(detail) : String(detail || err.message);
      console.error("[Render] Start failed", err, detail, err.stack);
      if (typeof detail === "object" && detail?.issues) {
        toast.error("Cannot start render", { description: detail.issues.join(" · ") + ` | VO vs Scenes check: ${msg}` });
      } else if (err.response?.status === 409) {
        toast.error("Render already running", { description: msg });
      } else {
        toast.error("Start failed: something went wrong please try again", { description: formatApiError(detail) || err.message + ` | ${msg}` });
      }
      // Surface server logs
      try {
        const pre = await api.get(`/projects/${projectId}/render/preflight`);
        console.log("[Render] Preflight", pre.data);
        if (!pre.data.ok) toast.error(`Preflight: ${pre.data.issues?.join(", ")}`);
      } catch {}
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

      {canEdit && (
        <div className="border border-zinc-800 bg-[#121212] rounded-sm p-4 space-y-4">
          <div>
            <div className="font-mono text-[10px] uppercase tracking-widest text-zinc-500 mb-3">Narrator voice — change until render</div>
            <VoiceStylePicker value={voiceStyle} onChange={setVoiceStyle} />
            <p className="font-mono text-[10px] text-zinc-600 mt-2">Voice selector stays enabled right up to the Render button, not locked after Part generation.</p>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4 pt-4 border-t border-zinc-800">
            <div>
              <div className="font-mono text-[10px] uppercase tracking-widest text-zinc-500 mb-2">Aspect ratio</div>
              <select value={aspect} onChange={e=>setAspect(e.target.value)} className="w-full bg-[#0A0A0A] border border-zinc-800 px-3 py-2 text-sm rounded-sm focus:border-[#00E5FF]">
                <option value="9:16">9:16 Tall - TikTok / Reels / Shorts</option>
                <option value="16:9">16:9 Wide - YouTube</option>
                <option value="1:1">1:1 Square - Feed</option>
              </select>
              <p className="font-mono text-[10px] text-zinc-600 mt-1">Choose at render — no need to regenerate.</p>
            </div>
            <div>
              <div className="font-mono text-[10px] uppercase tracking-widest text-zinc-500 mb-2">Captions</div>
              <label className="flex items-center gap-2 text-sm text-zinc-300">
                <input type="checkbox" checked={burnSubtitles} onChange={(e) => setBurnSubtitles(e.target.checked)} className="h-4 w-4 accent-[#00E5FF] bg-[#0A0A0A] border-zinc-700 rounded" />
                Burn captions into video
              </label>
              <p className="font-mono text-[10px] text-zinc-600 mt-1">Off = clean video. SRT still saved for CapCut. Platforms auto-caption anyway.</p>
            </div>
          </div>
        </div>
      )}

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

      {!activeJob && jobs[0] && (jobs[0].status === "cancelled" || jobs[0].status === "failed") && (
        <LastTerminalNotice job={jobs[0]} />
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
