import React, { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { toast } from "sonner";
import { ArrowLeft, Trash2, Loader2, Download } from "lucide-react";
import AppShell from "../components/AppShell";
import TopBar from "../components/TopBar";
import StatusBadge from "../components/StatusBadge";
import QualityScore from "../components/QualityScore";
import ScriptPanel from "../components/ScriptPanel";
import ScenePlanner from "../components/ScenePlanner";
import MetadataPanel from "../components/MetadataPanel";
import ThumbnailPanel from "../components/ThumbnailPanel";
import RenderPanel from "../components/RenderPanel";
import SharePanel from "../components/SharePanel";
import VoiceoverPanel from "../components/VoiceoverPanel";
import IntakePacket from "../components/IntakePacket";
import ClaimLedger from "../components/ClaimLedger";
import RightsRegistry from "../components/RightsRegistry";
import ComplianceChecklist from "../components/ComplianceChecklist";
import ApprovalPanel from "../components/ApprovalPanel";
import ProductionReceipt from "../components/ProductionReceipt";
import ProjectGoalsCard from "../components/ProjectGoalsCard";
import ProjectTabBar from "../components/ProjectTabBar";
import { api, formatApiError, API } from "../lib/api";
import { useAuth } from "../lib/auth";
import { formatCurrency, formatDuration } from "../lib/format";
import { useConfirm } from "../components/ConfirmDialog";

export default function ProjectDetailPage() {
  const { id } = useParams();
  const navigate = useNavigate();
  const { user } = useAuth();
  const [view, setView] = useState(null);
  const [loading, setLoading] = useState(true);
  const [tab, setTab] = useState("overview");
  const [deleting, setDeleting] = useState(false);
  const [autoGeneratingThumbnails, setAutoGeneratingThumbnails] = useState(false);
  const confirm = useConfirm();

  const canEdit = user && user.role !== "viewer" && (user.role === "admin" || !view || view.project?.user_id === user.id);

  const fetchView = async () => {
    try {
      const { data } = await api.get(`/projects/${id}`);
      setView(prev => {
        // Don't overwrite existing script with null from stale fetch (regression from last fix)
        if (prev?.script && !data?.script) {
          return { ...data, script: prev.script };
        }
        // Also persist script to localStorage for ScriptPanel recovery
        if (data?.script?.full_script) {
          try {
            localStorage.setItem(`script-${id}`, JSON.stringify(data.script));
          } catch {}
        }
        return data;
      });
    } catch (err) {
      toast.error("Could not load project", { description: formatApiError(err.response?.data?.detail) || err.message });
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchView();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  const remove = async () => {
    const ok = await confirm({
      title: "Delete this project?",
      description: "This permanently removes the project, script, scenes, metadata, assets, and any render jobs. This cannot be undone.",
      confirmLabel: "Delete project",
      tone: "destructive",
    });
    if (!ok) return;
    setDeleting(true);
    try {
      await api.delete(`/projects/${id}`);
      toast.success("Project deleted");
      navigate("/app/projects");
    } catch (err) {
      toast.error("Delete failed", { description: formatApiError(err.response?.data?.detail) || err.message });
      setDeleting(false);
    }
  };

  if (loading) {
    return (
      <AppShell>
        <TopBar title="Project" />
        <div className="p-8 text-sm text-zinc-500 font-mono">Loading…</div>
      </AppShell>
    );
  }
  if (!view) {
    return (
      <AppShell>
        <TopBar title="Project not found" />
        <div className="p-8">
          <button onClick={() => navigate("/app/projects")} className="text-sm text-[#00E5FF]">← Back</button>
        </div>
      </AppShell>
    );
  }

  // A mutation may return an incomplete payload (or a non-project response).
  // Never let it erase the last valid project and crash the entire page.
  if (!view?.project) {
    return (
      <AppShell>
        <TopBar title="Project unavailable" />
        <div className="p-8 space-y-4 text-sm text-zinc-300">
          <p>Project details could not be loaded. Your saved work has not been deleted.</p>
          <button type="button" onClick={fetchView} className="text-[#00E5FF] underline">
            Reload project
          </button>
        </div>
      </AppShell>
    );
  }

  const { project, script, scenes = [], metadata, assets = [], render_job, share, timing } = view;

  return (
    <AppShell>
      <TopBar
        title={project.name}
        subtitle={project.niche}
        right={
          <div className="flex items-center gap-2">
            <button
              onClick={() => navigate("/app/projects")}
              data-testid="back-to-projects"
              className="flex items-center gap-1 text-xs text-zinc-400 hover:text-white px-2 py-1"
            >
              <ArrowLeft size={12} /> All projects
            </button>
            {canEdit && (
              <button
                data-testid="delete-project-btn"
                onClick={remove}
                disabled={deleting}
                className="flex items-center gap-1.5 text-xs text-[#FF3366] hover:bg-[#FF3366]/10 border border-[#FF3366]/20 px-2 py-1.5 rounded-sm transition-colors disabled:opacity-50"
              >
                {deleting ? <Loader2 size={12} className="animate-spin" /> : <Trash2 size={12} />}
                Delete
              </button>
            )}
          </div>
        }
      />

      <div className="p-8 space-y-6 min-w-0 max-w-full">
        {/* Header summary */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
          <div className="lg:col-span-2 border border-[rgba(59,130,246,0.15)] bg-[#1c2333] p-6 rounded-sm space-y-4">
            <div className="flex items-center gap-3">
              <StatusBadge status={project.status} />
              <span className="font-mono text-[10px] text-zinc-500 uppercase tracking-widest">
                Target · {formatDuration(project.target_duration)}
              </span>
              <span className="font-mono text-[10px] text-zinc-500 uppercase tracking-widest">
                Est. cost · {formatCurrency(project.estimated_cost)}
              </span>
            </div>
            <p className="text-sm text-slate-300 leading-relaxed">{project.topic}</p>
            <div className="grid grid-cols-2 md:grid-cols-3 gap-4 pt-3 border-t border-[rgba(59,130,246,0.15)]">
              {[
                ["Who's watching? ⓘ", project.audience, "Who will watch this? This shapes voice + examples"],
                ["What's the goal? ⓘ", project.monetisation_intent, "What do you want this video to do?"],
                ["What should they do? ⓘ", project.cta_goal, "Main action you want viewer to take"],
                ["Tone", project.tone, null],
                ["Voice", project.voice_style, null],
                ["Visual", project.visual_style, null],
              ].map(([k, v, tip]) => (
                <div key={k} className="relative group">
                  <div className="font-mono text-[10px] uppercase tracking-widest text-slate-400 mb-1 flex items-center gap-1">{k}
                    {tip && <span className="inline-flex w-3 h-3 rounded-full bg-blue-500/20 border border-blue-400/40 text-blue-400 text-[8px] items-center justify-center">ⓘ</span>}
                  </div>
                  {tip && <span className="hidden group-hover:block absolute left-0 bottom-full mb-1 bg-slate-800 text-xs text-slate-100 px-2 py-1 rounded shadow z-10 whitespace-nowrap">{tip}</span>}
                  <div className="text-sm text-slate-200 truncate">{v}</div>
                </div>
              ))}
            </div>
          </div>

          <div className="border border-[rgba(59,130,246,0.15)] bg-[#1c2333] p-6 rounded-sm space-y-4">
            <QualityScore score={project.quality_score || 0} />
            <div className="pt-4 border-t border-[rgba(59,130,246,0.15)] flex items-center gap-3">
              <a
                data-testid="export-script-txt"
                href={`${API}/projects/${project.id}/export/script.txt`}
                className="flex-1 flex items-center justify-center gap-1.5 border border-[rgba(59,130,246,0.15)] text-slate-200 hover:text-[#00E5FF] hover:border-[#00E5FF] text-xs font-mono uppercase tracking-widest px-2 py-2 rounded-sm transition-colors"
              >
                <Download size={12} /> TXT
              </a>
              <a
                data-testid="export-scenes-csv-top"
                href={`${API}/projects/${project.id}/export/scenes.csv`}
                className="flex-1 flex items-center justify-center gap-1.5 border border-[rgba(59,130,246,0.15)] text-slate-200 hover:text-[#00E5FF] hover:border-[#00E5FF] text-xs font-mono uppercase tracking-widest px-2 py-2 rounded-sm transition-colors"
              >
                <Download size={12} /> CSV
              </a>
              <a
                data-testid="export-package-zip-top"
                href={`${API}/projects/${project.id}/export/package.zip`}
                className="flex-1 flex items-center justify-center gap-1.5 bg-[#00E5FF] text-black text-xs font-semibold font-mono uppercase tracking-widest px-2 py-2 rounded-sm hover:bg-[#33EFFF] transition-colors"
              >
                <Download size={12} /> ZIP
              </a>
            </div>
          </div>
        </div>

        <ProjectTabBar tab={tab} onChange={setTab} />

        {/* Tab content */}
        <div>
          {tab === "overview" && (
            <div className="space-y-4">
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <OverviewTile label="Script" ready={!!script} sub={script ? `${script.word_count} words` : "Not generated"} />
                <OverviewTile label="Scenes" ready={scenes.length > 0} sub={scenes.length > 0 ? `${scenes.length} scenes` : "Not generated"} />
                <OverviewTile label="Metadata" ready={!!metadata} sub={metadata ? `${metadata.title_options?.length || 0} titles` : "Not generated"} />
                <OverviewTile label="Thumbnails" ready={assets.some(a => a.asset_type === "thumbnail_concept")} sub={`${assets.filter(a => a.asset_type === "thumbnail_concept").length} concepts`} />
                <OverviewTile
                  label="Voiceover"
                  ready={assets.some(a => a.asset_type === "voiceover_audio")}
                  sub={(() => {
                    const vos = assets.filter(a => a.asset_type === "voiceover_audio");
                    const sceneVOs = vos.filter(a => a.scene_id);
                    const fullVO = vos.find(a => !a.scene_id && a.id === project.selected_voiceover_asset_id);
                    if (vos.length === 0) return "Not generated";
                    return `${sceneVOs.length}/${scenes.length} scenes${fullVO ? " · full ready" : ""}`;
                  })()}
                />
                <OverviewTile
                  label="Final video"
                  ready={!!render_job && String(render_job.status || "").toLowerCase() === "completed" && !!(render_job.output_storage_key || render_job.output_url)}
                  sub={
                    render_job && String(render_job.status || "").toLowerCase() === "completed" && (render_job.output_storage_key || render_job.output_url)
                      ? `MP4 ready · ${render_job.duration ? `${Math.round(render_job.duration)}s` : ""}`
                      : render_job && ["queued","validating","preparing_assets","rendering","verifying"].includes(String(render_job.status || "").toLowerCase())
                      ? `Rendering · ${render_job.progress || 0}%`
                      : "Not rendered"
                  }
                />
              </div>
              {String(render_job?.status || "").toLowerCase() === "completed" && (render_job.output_storage_key || render_job.output_url) && (
                <div data-testid="overview-final-video" className="border border-[#00FF66]/30 bg-[#00FF66]/5 rounded-sm p-4 space-y-3">
                  <div className="font-mono text-[10px] uppercase tracking-widest text-[#00FF66]">
                    Final video · ready
                  </div>
                  <video
                    controls
                    src={`${API}/projects/${project.id}/render/jobs/${render_job.id}/download`}
                    poster={assets.find(a => a.id === project.selected_thumbnail_asset_id)?.preview_url}
                    preload="metadata"
                    className="w-full rounded-sm border border-[rgba(59,130,246,0.15)] bg-black aspect-video"
                  />
                </div>
              )}
              <ProjectGoalsCard projectId={project.id} project={project} canEdit={canEdit} onChange={setView} />
              <SharePanel
                projectId={project.id}
                share={share}
                projectStatus={project.status}
                canEdit={canEdit}
                onChange={fetchView}
              />
            </div>
          )}
          {tab === "script" && (
            <ScriptPanel projectId={project.id} script={script} canEdit={canEdit} onChange={setView} />
          )}
          {tab === "scenes" && (
            <ScenePlanner projectId={project.id} project={project} scenes={scenes} canEdit={canEdit} onChange={setView} hasScript={!!script} attachedAssets={assets} />
          )}
          {tab === "metadata" && (
            <MetadataPanel
              projectId={project.id}
              metadata={metadata}
              canEdit={canEdit}
              onChange={setView}
              hasScript={!!script}
              onThumbnailGeneratingChange={setAutoGeneratingThumbnails}
            />
          )}
          {tab === "thumbnails" && (
            <ThumbnailPanel
              projectId={project.id}
              assets={assets}
              selectedThumbnailId={project.selected_thumbnail_asset_id}
              canEdit={canEdit}
              onChange={setView}
              autoGeneratingConcepts={autoGeneratingThumbnails}
            />
          )}
          {tab === "voiceover" && (
            <VoiceoverPanel
              projectId={project.id}
              project={project}
              script={script}
              scenes={scenes}
              assets={assets}
              hasScript={!!script}
              canEdit={canEdit}
              onChange={(data) => {
                if (data?.project) setView(data);
                else fetchView();
              }}
            />
          )}
          {tab === "render" && (
            <RenderPanel
              projectId={project.id}
              project={project}
              script={script}
              scenes={scenes}
              metadata={metadata}
              assets={assets}
              timing={timing}
              canEdit={canEdit}
              onChange={setView}
            />
          )}
          {tab === "intake" && <IntakePacket projectId={project.id} />}
          {tab === "claims" && <ClaimLedger projectId={project.id} />}
          {tab === "rights" && <RightsRegistry projectId={project.id} />}
          {tab === "compliance" && <ComplianceChecklist projectId={project.id} />}
          {tab === "approval" && <ApprovalPanel projectId={project.id} jobId={render_job?.id} />}
          {tab === "receipt" && <ProductionReceipt projectId={project.id} />}
        </div>
      </div>
    </AppShell>
  );
}

function OverviewTile({ label, ready, sub }) {
  return (
    <div className="border border-[rgba(59,130,246,0.15)] bg-[#1c2333] p-5 rounded-sm">
      <div className="flex items-center justify-between mb-4">
        <span className="font-mono text-[10px] uppercase tracking-widest text-zinc-500">{label}</span>
        <span
          className="font-mono text-[10px] uppercase tracking-widest"
          style={{ color: ready ? "#00FF66" : "#71717A" }}
        >
          {ready ? "Ready" : "Pending"}
        </span>
      </div>
      <div className="text-sm text-slate-200">{sub}</div>
    </div>
  );
}
