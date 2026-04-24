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
import { api, formatApiError, API } from "../lib/api";
import { useAuth } from "../lib/auth";
import { formatCurrency, formatDuration } from "../lib/format";
import { useConfirm } from "../components/ConfirmDialog";

const TABS = [
  { id: "overview", label: "Overview" },
  { id: "script", label: "Script" },
  { id: "scenes", label: "Scenes" },
  { id: "metadata", label: "Metadata" },
  { id: "thumbnails", label: "Thumbnails" },
  { id: "render", label: "Render & Export" },
];

export default function ProjectDetailPage() {
  const { id } = useParams();
  const navigate = useNavigate();
  const { user } = useAuth();
  const [view, setView] = useState(null);
  const [loading, setLoading] = useState(true);
  const [tab, setTab] = useState("overview");
  const [deleting, setDeleting] = useState(false);
  const confirm = useConfirm();

  const canEdit = user && user.role !== "viewer";

  const fetchView = async () => {
    try {
      const { data } = await api.get(`/projects/${id}`);
      setView(data);
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

  const { project, script, scenes, metadata, assets, render_job, share } = view;

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

      <div className="p-8 space-y-6">
        {/* Header summary */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
          <div className="lg:col-span-2 border border-zinc-800 bg-[#121212] p-6 rounded-sm space-y-4">
            <div className="flex items-center gap-3">
              <StatusBadge status={project.status} />
              <span className="font-mono text-[10px] text-zinc-500 uppercase tracking-widest">
                Target · {formatDuration(project.target_duration)}
              </span>
              <span className="font-mono text-[10px] text-zinc-500 uppercase tracking-widest">
                Est. cost · {formatCurrency(project.estimated_cost)}
              </span>
            </div>
            <p className="text-sm text-zinc-300 leading-relaxed">{project.topic}</p>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4 pt-3 border-t border-zinc-800">
              {[
                ["Audience", project.audience],
                ["Tone", project.tone],
                ["Voice", project.voice_style],
                ["Visual", project.visual_style],
                ["Monetisation", project.monetisation_intent],
                ["CTA", project.cta_goal],
              ].map(([k, v]) => (
                <div key={k}>
                  <div className="font-mono text-[10px] uppercase tracking-widest text-zinc-500 mb-1">{k}</div>
                  <div className="text-sm text-zinc-200 truncate">{v}</div>
                </div>
              ))}
            </div>
          </div>

          <div className="border border-zinc-800 bg-[#121212] p-6 rounded-sm space-y-4">
            <QualityScore score={project.quality_score || 0} />
            <div className="pt-4 border-t border-zinc-800 flex items-center gap-3">
              <a
                data-testid="export-script-txt"
                href={`${API}/projects/${project.id}/export/script.txt`}
                className="flex-1 flex items-center justify-center gap-1.5 border border-zinc-800 text-zinc-300 hover:text-[#00E5FF] hover:border-[#00E5FF] text-xs font-mono uppercase tracking-widest px-2 py-2 rounded-sm transition-colors"
              >
                <Download size={12} /> TXT
              </a>
              <a
                data-testid="export-scenes-csv-top"
                href={`${API}/projects/${project.id}/export/scenes.csv`}
                className="flex-1 flex items-center justify-center gap-1.5 border border-zinc-800 text-zinc-300 hover:text-[#00E5FF] hover:border-[#00E5FF] text-xs font-mono uppercase tracking-widest px-2 py-2 rounded-sm transition-colors"
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

        {/* Tabs */}
        <div className="border-b border-zinc-800 flex items-center gap-1 overflow-x-auto">
          {TABS.map((t) => (
            <button
              key={t.id}
              data-testid={`tab-${t.id}`}
              onClick={() => setTab(t.id)}
              className={`px-4 py-3 text-sm font-medium border-b-2 transition-colors whitespace-nowrap ${
                tab === t.id
                  ? "border-[#00E5FF] text-white"
                  : "border-transparent text-zinc-500 hover:text-white"
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>

        {/* Tab content */}
        <div>
          {tab === "overview" && (
            <div className="space-y-4">
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <OverviewTile label="Script" ready={!!script} sub={script ? `${script.word_count} words` : "Not generated"} />
                <OverviewTile label="Scenes" ready={scenes.length > 0} sub={scenes.length > 0 ? `${scenes.length} scenes` : "Not generated"} />
                <OverviewTile label="Metadata" ready={!!metadata} sub={metadata ? `${metadata.title_options?.length || 0} titles` : "Not generated"} />
                <OverviewTile label="Thumbnails" ready={assets.some(a => a.asset_type === "thumbnail_concept")} sub={`${assets.filter(a => a.asset_type === "thumbnail_concept").length} concepts`} />
              </div>
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
            <ScenePlanner projectId={project.id} scenes={scenes} canEdit={canEdit} onChange={setView} hasScript={!!script} />
          )}
          {tab === "metadata" && (
            <MetadataPanel projectId={project.id} metadata={metadata} canEdit={canEdit} onChange={setView} hasScript={!!script} />
          )}
          {tab === "thumbnails" && (
            <ThumbnailPanel projectId={project.id} assets={assets} canEdit={canEdit} onChange={setView} />
          )}
          {tab === "render" && (
            <RenderPanel
              projectId={project.id}
              project={project}
              script={script}
              scenes={scenes}
              metadata={metadata}
              assets={assets}
              renderJob={render_job}
              canEdit={canEdit}
              onChange={setView}
            />
          )}
        </div>
      </div>
    </AppShell>
  );
}

function OverviewTile({ label, ready, sub }) {
  return (
    <div className="border border-zinc-800 bg-[#121212] p-5 rounded-sm">
      <div className="flex items-center justify-between mb-4">
        <span className="font-mono text-[10px] uppercase tracking-widest text-zinc-500">{label}</span>
        <span
          className="font-mono text-[10px] uppercase tracking-widest"
          style={{ color: ready ? "#00FF66" : "#71717A" }}
        >
          {ready ? "Ready" : "Pending"}
        </span>
      </div>
      <div className="text-sm text-zinc-300">{sub}</div>
    </div>
  );
}
