import React, { useEffect, useState } from "react";
import AppShell from "../components/AppShell";
import TopBar from "../components/TopBar";
import { api, formatApiError } from "../lib/api";
import { Link } from "react-router-dom";
import { Boxes, ExternalLink, Trash2, Loader2 } from "lucide-react";
import { toast } from "sonner";
import { useAuth } from "../lib/auth";
import { useConfirm } from "../components/ConfirmDialog";

const TYPE_META = {
  stock_video: { label: "Video · Pexels", color: "#7B61FF" },
  stock_image: { label: "Photo · Pexels", color: "#00E5FF" },
  thumbnail_concept: { label: "Thumbnail brief", color: "#FFB020" },
  generated_thumbnail: { label: "Thumbnail · image", color: "#00FF66" },
  voiceover_audio: { label: "Voiceover · audio", color: "#FF66CC" },
};

const FILTERS = [
  { id: "all", label: "All" },
  { id: "stock", label: "Stock" },
  { id: "thumbnail_concept", label: "Thumbnails" },
  { id: "voiceover_audio", label: "Voiceover" },
];

export default function AssetLibraryPage() {
  const { user } = useAuth();
  const confirm = useConfirm();
  const canEdit = user && user.role !== "viewer";
  const [projects, setProjects] = useState([]);
  const [loading, setLoading] = useState(true);
  const [details, setDetails] = useState({});
  const [filter, setFilter] = useState("all");
  const [removing, setRemoving] = useState(null);

  const fetchAll = async () => {
    setLoading(true);
    try {
      const { data } = await api.get("/projects");
      setProjects(data);
      const results = await Promise.all(
        data.map((p) => api.get(`/projects/${p.id}`).then((r) => [p.id, r.data]).catch(() => [p.id, null]))
      );
      const map = {};
      results.forEach(([id, v]) => { if (v) map[id] = v; });
      setDetails(map);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { fetchAll(); }, []);

  const remove = async (projectId, asset) => {
    const ok = await confirm({
      title: "Remove this asset?",
      description: "It will be detached from its scene and deleted.",
      confirmLabel: "Remove",
      tone: "destructive",
    });
    if (!ok) return;
    setRemoving(asset.id);
    try {
      await api.delete(`/projects/${projectId}/assets/${asset.id}`);
      toast.success("Asset removed");
      await fetchAll();
    } catch (err) {
      toast.error("Remove failed", { description: formatApiError(err.response?.data?.detail) || err.message });
    } finally {
      setRemoving(null);
    }
  };

  if (loading) {
    return (
      <AppShell>
        <TopBar title="Asset Library" />
        <div className="p-8 text-sm text-zinc-500 font-mono">Loading…</div>
      </AppShell>
    );
  }

  const allAssets = [];
  Object.entries(details).forEach(([pid, v]) => {
    (v.assets || []).forEach((a) => {
      const scene = (v.scenes || []).find((s) => s.id === a.scene_id);
      allAssets.push({ ...a, projectName: v.project.name, projectId: pid, sceneNumber: scene?.scene_number });
    });
  });

  const visible = allAssets.filter((a) => {
    if (filter === "all") return true;
    if (filter === "stock") return a.asset_type === "stock_video" || a.asset_type === "stock_image";
    return a.asset_type === filter;
  });

  return (
    <AppShell>
      <TopBar title="Asset Library" subtitle={`${allAssets.length} items`} />
      <div className="p-8 space-y-6">
        <div className="flex border border-zinc-800 rounded-sm overflow-hidden w-fit">
          {FILTERS.map((f) => (
            <button
              key={f.id}
              data-testid={`asset-filter-${f.id}`}
              onClick={() => setFilter(f.id)}
              className={`px-4 py-2 font-mono text-[10px] uppercase tracking-widest transition-colors ${
                filter === f.id ? "bg-[#00E5FF] text-black" : "text-zinc-400 hover:text-white hover:bg-[#1A1A1A]"
              }`}
            >
              {f.label}
            </button>
          ))}
        </div>

        {visible.length === 0 ? (
          <div className="border border-zinc-800 border-dashed p-10 text-center rounded-sm">
            <Boxes size={24} strokeWidth={1.2} className="mx-auto mb-3 text-zinc-500" />
            <p className="text-sm text-zinc-400">
              No {filter === "all" ? "assets" : filter} yet. Generate thumbnail concepts or use "Find Assets" on a scene.
            </p>
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
            {visible.map((a) => {
              const meta = TYPE_META[a.asset_type] || { label: a.asset_type, color: "#A1A1AA" };
              const isStock = a.asset_type === "stock_video" || a.asset_type === "stock_image";
              const isVoiceover = a.asset_type === "voiceover_audio";
              const isThumbnailImage = a.asset_type === "generated_thumbnail";
              return (
                <div
                  key={a.id}
                  data-testid={`asset-card-${a.id}`}
                  className="border border-zinc-800 bg-[#121212] rounded-sm overflow-hidden flex flex-col"
                >
                  {isStock && a.preview_url ? (
                    <div className="relative aspect-video bg-[#1A1A1A] overflow-hidden">
                      <img
                        src={a.preview_url}
                        alt={a.name || ""}
                        className="w-full h-full object-cover"
                        loading="lazy"
                        referrerPolicy="no-referrer"
                      />
                      <span
                        className="absolute top-2 left-2 font-mono text-[9px] uppercase tracking-widest px-1.5 py-0.5 rounded-sm border"
                        style={{
                          color: meta.color,
                          background: meta.color + "15",
                          borderColor: meta.color + "4D",
                        }}
                      >
                        {meta.label}
                      </span>
                      {a.duration ? (
                        <span className="absolute top-2 right-2 font-mono text-[9px] text-white bg-black/70 px-1.5 py-0.5 rounded-sm">
                          {a.duration}s
                        </span>
                      ) : null}
                    </div>
                  ) : isThumbnailImage && a.preview_url ? (
                    <div className="relative aspect-video bg-[#1A1A1A] overflow-hidden">
                      <img
                        src={a.preview_url}
                        alt={a.name || ""}
                        className="w-full h-full object-cover"
                        loading="lazy"
                      />
                      <span
                        className="absolute top-2 left-2 font-mono text-[9px] uppercase tracking-widest px-1.5 py-0.5 rounded-sm border"
                        style={{
                          color: meta.color,
                          background: meta.color + "15",
                          borderColor: meta.color + "4D",
                        }}
                      >
                        {meta.label}
                      </span>
                    </div>
                  ) : isVoiceover ? (
                    <div className="relative bg-[#1A1A1A] p-4 space-y-2">
                      <span
                        className="inline-block font-mono text-[9px] uppercase tracking-widest px-1.5 py-0.5 rounded-sm border"
                        style={{
                          color: meta.color,
                          background: meta.color + "15",
                          borderColor: meta.color + "4D",
                        }}
                      >
                        {meta.label}
                      </span>
                      <audio controls src={a.preview_url} className="w-full h-9" preload="metadata" />
                    </div>
                  ) : (
                    <div className="aspect-video bg-[#1A1A1A] flex items-center justify-center">
                      <span
                        className="font-mono text-[10px] uppercase tracking-widest px-2 py-1 rounded-sm border"
                        style={{ color: meta.color, borderColor: meta.color + "4D" }}
                      >
                        {meta.label}
                      </span>
                    </div>
                  )}

                  <div className="p-4 space-y-2 flex-1">
                    <div className="flex items-start justify-between gap-2">
                      <h4 className="text-sm font-semibold text-white truncate flex-1">{a.name}</h4>
                      <span className="font-mono text-[10px] uppercase tracking-widest text-[#00FF66]">
                        {a.status}
                      </span>
                    </div>
                    <div className="font-mono text-[10px] uppercase tracking-widest text-zinc-500">
                      {a.projectName}
                      {a.sceneNumber && (
                        <> · <span className="text-[#00E5FF]">Scene {String(a.sceneNumber).padStart(2, "0")}</span></>
                      )}
                    </div>
                    {a.attribution_name && (
                      <div className="font-mono text-[10px] text-zinc-500">
                        by <span className="text-[#7B61FF]">{a.attribution_name}</span>
                        {a.width && a.height ? (
                          <span className="text-zinc-600"> · {a.width}×{a.height}</span>
                        ) : null}
                      </div>
                    )}
                    {a.brief?.thumbnail_title_text && (
                      <div className="pt-2 border-t border-zinc-800 text-xs text-zinc-300">
                        "{a.brief.thumbnail_title_text}"
                      </div>
                    )}
                  </div>

                  <div className="px-4 py-2 border-t border-zinc-800 flex items-center gap-2">
                    <Link
                      to={`/app/projects/${a.projectId}`}
                      data-testid={`asset-open-project-${a.id}`}
                      className="flex-1 text-center font-mono text-[10px] uppercase tracking-widest text-zinc-400 hover:text-[#00E5FF] px-2 py-1.5 rounded-sm transition-colors"
                    >
                      Open project
                    </Link>
                    {a.source_url && (
                      <a
                        href={a.source_url}
                        target="_blank"
                        rel="noreferrer"
                        data-testid={`asset-source-${a.id}`}
                        className="font-mono text-[10px] uppercase tracking-widest text-zinc-400 hover:text-[#00E5FF] border border-zinc-800 hover:border-[#00E5FF] px-2 py-1.5 rounded-sm transition-colors flex items-center gap-1"
                      >
                        <ExternalLink size={11} strokeWidth={1.5} /> Source
                      </a>
                    )}
                    {canEdit && (isStock || isVoiceover) && (
                      <button
                        data-testid={`asset-remove-${a.id}`}
                        onClick={() => remove(a.projectId, a)}
                        disabled={removing === a.id}
                        className="font-mono text-[10px] uppercase tracking-widest text-zinc-400 hover:text-[#FF3366] border border-zinc-800 hover:border-[#FF3366] px-2 py-1.5 rounded-sm transition-colors flex items-center gap-1 disabled:opacity-50"
                      >
                        {removing === a.id ? <Loader2 size={11} className="animate-spin" /> : <Trash2 size={11} strokeWidth={1.5} />}
                      </button>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </AppShell>
  );
}
