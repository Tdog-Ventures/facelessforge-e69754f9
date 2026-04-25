import React, { useState } from "react";
import { toast } from "sonner";
import { Sparkles, Loader2, Download, Search, X as XIcon, Wand2 } from "lucide-react";
import { api, formatApiError, API } from "../lib/api";
import { formatDuration } from "../lib/format";
import StockAssetModal from "./StockAssetModal";
import { useConfirm } from "./ConfirmDialog";

export default function ScenePlanner({ projectId, scenes, canEdit, onChange, hasScript, attachedAssets = [] }) {
  const [generating, setGenerating] = useState(false);
  const [autoAttaching, setAutoAttaching] = useState(false);
  const [activeScene, setActiveScene] = useState(null);
  const confirm = useConfirm();

  const generate = async () => {
    setGenerating(true);
    toast.loading("Generating scene plan…", { id: "gen-scenes" });
    try {
      const { data } = await api.post(`/projects/${projectId}/generate-scenes`);
      onChange(data);
      toast.success("Scenes generated", { id: "gen-scenes" });
    } catch (err) {
      toast.error("Generation failed", { id: "gen-scenes", description: formatApiError(err.response?.data?.detail) || err.message });
    } finally {
      setGenerating(false);
    }
  };

  const exportCsv = () => {
    window.open(`${API}/projects/${projectId}/export/scenes.csv`, "_blank");
  };

  const detach = async (asset) => {
    try {
      await api.delete(`/projects/${projectId}/assets/${asset.id}`);
      toast.success("Asset detached");
      const { data } = await api.get(`/projects/${projectId}`);
      onChange(data);
    } catch (err) {
      toast.error("Detach failed", { description: formatApiError(err.response?.data?.detail) || err.message });
    }
  };

  const assetsForScene = (sceneId) =>
    (attachedAssets || []).filter(
      (a) => a.scene_id === sceneId && (a.asset_type === "stock_video" || a.asset_type === "stock_image")
    );

  const voiceoverForScene = (sceneId) => {
    const list = (attachedAssets || []).filter(
      (a) => a.scene_id === sceneId && a.asset_type === "voiceover_audio"
    );
    if (list.length === 0) return null;
    return list.find((v) => v.status === "selected") || list[0];
  };

  const scenesWithAssets = (scenes || []).filter((s) => assetsForScene(s.id).length > 0).length;
  const scenesWithoutAssets = (scenes || []).length - scenesWithAssets;

  const autoAttach = async () => {
    let replaceExisting = false;
    if (scenesWithAssets > 0) {
      const ok = await confirm({
        title: "Replace existing scene assets?",
        description: `${scenesWithAssets} scenes already have assets. Choose Replace to rebuild everything, or Cancel to only fill the ${scenesWithoutAssets} empty scenes.`,
        confirmLabel: "Replace all",
        cancelLabel: scenesWithoutAssets > 0 ? `Fill ${scenesWithoutAssets} empty only` : "Cancel",
        tone: "warning",
      });
      replaceExisting = !!ok;
      if (!ok && scenesWithoutAssets === 0) return; // nothing to do
    }
    setAutoAttaching(true);
    const toastId = toast.loading(
      `Auto-attaching to ${replaceExisting ? "all" : scenesWithoutAssets} scenes…`,
      { id: "auto-attach" },
    );
    try {
      const { data } = await api.post(`/projects/${projectId}/auto-attach-assets`, {
        replace_existing: replaceExisting,
        media_type: "both",
      });
      const parts = [
        `${data.attached} attached`,
        `${data.skipped} skipped`,
        `${data.failed} failed`,
      ];
      if (data.attached > 0) {
        toast.success(`Auto-attach complete`, {
          id: toastId,
          description: parts.join(" · "),
        });
      } else {
        toast.info("Auto-attach finished", { id: toastId, description: parts.join(" · ") });
      }
      const { data: refreshed } = await api.get(`/projects/${projectId}`);
      onChange(refreshed);
    } catch (err) {
      toast.error("Auto-attach failed", { id: toastId, description: formatApiError(err.response?.data?.detail) || err.message });
    } finally {
      setAutoAttaching(false);
    }
  };

  if (!scenes || scenes.length === 0) {
    return (
      <div className="border border-zinc-800 border-dashed p-10 text-center rounded-sm">
        <p className="text-sm text-zinc-400 mb-4">
          {hasScript ? "No scene plan yet. Generate one from the script." : "Generate a script first, then break it into scenes."}
        </p>
        {canEdit && hasScript && (
          <button
            data-testid="generate-scenes-btn"
            onClick={generate}
            disabled={generating}
            className="inline-flex items-center gap-2 bg-[#00E5FF] text-black font-semibold text-sm px-4 py-2 rounded-sm hover:bg-[#33EFFF] disabled:opacity-60 transition-colors"
          >
            {generating ? <Loader2 size={14} className="animate-spin" /> : <Sparkles size={14} strokeWidth={2} />}
            {generating ? "Generating…" : "Generate Scenes"}
          </button>
        )}
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div className="font-mono text-[11px] text-zinc-500">
          {scenes.length} scenes · total {formatDuration(scenes[scenes.length - 1]?.end_time || 0)}
        </div>
        <div className="flex items-center gap-2">
          {canEdit && (
            <button
              data-testid="auto-attach-btn"
              onClick={autoAttach}
              disabled={autoAttaching}
              className="flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-widest text-black bg-[#00FF66] hover:bg-[#33FF80] px-2.5 py-1 rounded-sm transition-colors disabled:opacity-60"
            >
              {autoAttaching ? <Loader2 size={12} className="animate-spin" /> : <Wand2 size={12} strokeWidth={1.8} />}
              {autoAttaching ? "Auto-attaching…" : "Auto-attach Assets"}
            </button>
          )}
          <button
            data-testid="export-scenes-csv"
            onClick={exportCsv}
            className="flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-widest text-zinc-400 hover:text-[#00E5FF] border border-zinc-800 hover:border-[#00E5FF] px-2 py-1 rounded-sm transition-colors"
          >
            <Download size={12} strokeWidth={1.5} /> CSV
          </button>
          {canEdit && (
            <button
              data-testid="regenerate-scenes-btn"
              onClick={generate}
              disabled={generating}
              className="flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-widest text-zinc-400 hover:text-[#00E5FF] border border-zinc-800 hover:border-[#00E5FF] px-2 py-1 rounded-sm transition-colors disabled:opacity-50"
            >
              {generating ? <Loader2 size={12} className="animate-spin" /> : <Sparkles size={12} strokeWidth={1.5} />}
              Regenerate
            </button>
          )}
        </div>
      </div>

      <div className="border border-zinc-800 bg-[#121212] rounded-sm overflow-hidden">
        <table className="w-full text-sm">
          <thead className="border-b border-zinc-800 bg-[#0A0A0A]">
            <tr className="font-mono text-[10px] text-zinc-500 uppercase tracking-widest">
              <th className="text-left px-4 py-3 w-12">#</th>
              <th className="text-left px-4 py-3 w-28">Time</th>
              <th className="text-left px-4 py-3">Narration</th>
              <th className="text-left px-4 py-3 w-64">Visual · asset</th>
              <th className="text-left px-4 py-3 w-56">Caption & assets</th>
            </tr>
          </thead>
          <tbody>
            {scenes.map((s) => {
              const sceneAssets = assetsForScene(s.id);
              return (
                <tr key={s.id} className="border-b border-zinc-800 hover:bg-[#1A1A1A] transition-colors align-top">
                  <td className="px-4 py-3 font-mono text-[#00E5FF] text-xs">{String(s.scene_number).padStart(2, "0")}</td>
                  <td className="px-4 py-3 font-mono text-xs text-zinc-400">
                    {formatDuration(s.start_time)} → {formatDuration(s.end_time)}
                  </td>
                  <td className="px-4 py-3 text-zinc-200 leading-relaxed">
                    {s.narration_text}
                    {(() => {
                      const v = voiceoverForScene(s.id);
                      if (!v) return null;
                      return (
                        <div data-testid={`scene-voiceover-${s.id}`} className="mt-2 flex items-center gap-2">
                          <span
                            className="font-mono text-[9px] uppercase tracking-widest px-1.5 py-0.5 rounded-sm border"
                            style={{
                              color: "#FF66CC",
                              background: "rgba(255,102,204,0.1)",
                              borderColor: "rgba(255,102,204,0.4)",
                            }}
                          >
                            VO · {v.voice_style}
                          </span>
                          <audio
                            src={v.preview_url}
                            controls
                            preload="none"
                            className="h-8 max-w-[260px]"
                          />
                        </div>
                      );
                    })()}
                  </td>
                  <td className="px-4 py-3">
                    <div className="text-zinc-300 mb-1">{s.visual_direction}</div>
                    <div className="font-mono text-[10px] uppercase tracking-widest text-[#7B61FF]">
                      {s.asset_type.replace(/_/g, " ")}
                    </div>
                    {s.search_terms?.length > 0 && (
                      <div className="mt-2 flex flex-wrap gap-1">
                        {s.search_terms.map((t, i) => (
                          <span key={i} className="font-mono text-[10px] px-1.5 py-0.5 border border-zinc-800 text-zinc-400 rounded-sm">
                            {t}
                          </span>
                        ))}
                      </div>
                    )}
                  </td>
                  <td className="px-4 py-3 space-y-2">
                    <div className="text-zinc-300 italic">"{s.caption_text}"</div>
                    {sceneAssets.length > 0 && (
                      <div className="flex flex-wrap gap-1.5">
                        {sceneAssets.map((a) => (
                          <div
                            key={a.id}
                            data-testid={`scene-asset-${a.id}`}
                            className="group relative w-16 h-10 overflow-hidden border border-[#00FF66]/30 rounded-sm"
                            title={`${a.name} · ${a.attribution_name || ""}`}
                          >
                            {a.preview_url ? (
                              <img src={a.preview_url} alt="" className="w-full h-full object-cover" loading="lazy" referrerPolicy="no-referrer" />
                            ) : (
                              <div className="w-full h-full bg-[#1A1A1A]" />
                            )}
                            {canEdit && (
                              <button
                                data-testid={`detach-asset-${a.id}`}
                                onClick={() => detach(a)}
                                className="absolute inset-0 bg-black/70 opacity-0 group-hover:opacity-100 flex items-center justify-center text-[#FF3366] transition-opacity"
                              >
                                <XIcon size={14} strokeWidth={2} />
                              </button>
                            )}
                          </div>
                        ))}
                      </div>
                    )}
                    {canEdit && (
                      <button
                        data-testid={`find-assets-btn-${s.id}`}
                        onClick={() => setActiveScene(s)}
                        className="inline-flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-widest text-[#00E5FF] hover:text-white border border-[#00E5FF]/30 hover:border-[#00E5FF] px-2 py-1 rounded-sm transition-colors"
                      >
                        <Search size={11} strokeWidth={1.5} />
                        {sceneAssets.length > 0 ? "Find more" : "Find Assets"}
                      </button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {activeScene && (
        <StockAssetModal
          open={!!activeScene}
          onOpenChange={(o) => !o && setActiveScene(null)}
          projectId={projectId}
          scene={activeScene}
          onAttached={async () => {
            const { data } = await api.get(`/projects/${projectId}`);
            onChange(data);
          }}
        />
      )}
    </div>
  );
}

