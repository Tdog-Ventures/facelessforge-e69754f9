import React, { useEffect, useState } from "react";
import { toast } from "sonner";
import {
  Sparkles, Loader2, Copy, Check, Star, X as XIcon, ExternalLink, Image as ImageIcon, Wand2,
} from "lucide-react";
import { api, formatApiError } from "../lib/api";
import { useConfirm } from "./ConfirmDialog";

export default function ThumbnailPanel({
  projectId,
  assets,
  selectedThumbnailId,
  canEdit,
  onChange,
  autoGeneratingConcepts = false,
}) {
  const [generating, setGenerating] = useState(null); // brief id currently generating
  const [working, setWorking] = useState(null); // asset id being acted on
  const [autoSelectFirst, setAutoSelectFirst] = useState(true);
  const [autoPendingBriefId, setAutoPendingBriefId] = useState(null);
  const [meta, setMeta] = useState({ mock: true, provider: "gemini_nano_banana" });
  const [metaLoading, setMetaLoading] = useState(true);
  const [metaError, setMetaError] = useState(null);
  const confirm = useConfirm();

  // Derived lists — defined before any effect that reads them so the hooks never
  // close over an uninitialized variable (and so the panel never crashes when
  // assets arrive late or missing).
  const safeAssets = (assets || []).filter(Boolean);
  const briefs = safeAssets.filter((a) => a?.asset_type === "thumbnail_concept" && a?.brief);
  const generatedByBrief = (briefId) =>
    safeAssets.filter((a) => a?.asset_type === "generated_thumbnail" && a?.brief_asset_id === briefId);

  const loadMeta = async () => {
    setMetaLoading(true);
    setMetaError(null);
    try {
      const { data } = await api.get("/thumbnails/meta");
      setMeta(data || { mock: true, provider: "gemini_nano_banana" });
    } catch (err) {
      setMetaError(formatApiError(err.response?.data?.detail) || err.message);
    } finally {
      setMetaLoading(false);
    }
  };

  useEffect(() => {
    loadMeta();
  }, []);

  // Auto-select the first thumbnail concept if the user hasn't picked one within 5 seconds.
  useEffect(() => {
    if (!canEdit || !autoSelectFirst || selectedThumbnailId || autoGeneratingConcepts || !briefs.length) return;
    const firstBrief = briefs[0];
    const firstGens = generatedByBrief(firstBrief.id);
    if (firstGens.length > 0) {
      const timer = setTimeout(() => {
        if (!selectedThumbnailId) select(firstGens[0].id);
      }, 5000);
      return () => clearTimeout(timer);
    }
    // No generated image yet — kick off generation for the first brief and mark it pending.
    const timer = setTimeout(() => {
      if (!selectedThumbnailId) {
        setAutoPendingBriefId(firstBrief.id);
        generate(firstBrief.id, 1);
      }
    }, 5000);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [briefs.length, selectedThumbnailId, autoSelectFirst, canEdit, autoGeneratingConcepts]);

  // After generating the first brief, select its first variant.
  useEffect(() => {
    if (!autoPendingBriefId || selectedThumbnailId) return;
    const gens = generatedByBrief(autoPendingBriefId);
    if (gens.length > 0) {
      select(gens[0].id);
      setAutoPendingBriefId(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [assets, autoPendingBriefId, selectedThumbnailId]);

  const generate = async (briefId, variants = 1) => {
    if (!projectId || !briefId) return;
    setGenerating(briefId);
    const toastId = toast.loading(
      variants > 1 ? `Generating ${variants} thumbnail variants…` : "Generating thumbnail image…",
      { id: `gen-thumb-${briefId}` },
    );
    try {
      const { data } = await api.post(
        `/projects/${projectId}/thumbnails/${briefId}/generate`,
        { variants },
      );
      onChange(data);
      const newCount = (data.assets || []).filter(
        (a) => a.asset_type === "generated_thumbnail" && a.brief_asset_id === briefId,
      ).length;
      toast.success(
        `${variants > 1 ? `${variants} variants ready` : "Thumbnail generated"}`,
        { id: toastId, description: `${newCount} total for this concept` },
      );
    } catch (err) {
      toast.error("Generation failed", {
        id: toastId,
        description: formatApiError(err.response?.data?.detail) || err.message,
      });
    } finally {
      setGenerating(null);
    }
  };

  const select = async (assetId) => {
    if (!projectId || !assetId) return;
    setWorking(assetId);
    try {
      const { data } = await api.post(`/projects/${projectId}/thumbnails/${assetId}/select`);
      onChange(data);
      toast.success("Marked as selected");
    } catch (err) {
      toast.error("Select failed", { description: formatApiError(err.response?.data?.detail) || err.message });
    } finally {
      setWorking(null);
    }
  };

  const reject = async (assetId) => {
    if (!projectId || !assetId) return;
    setWorking(assetId);
    try {
      const { data } = await api.post(`/projects/${projectId}/thumbnails/${assetId}/reject`);
      onChange(data);
      toast.success("Rejected");
    } catch (err) {
      toast.error("Reject failed", { description: formatApiError(err.response?.data?.detail) || err.message });
    } finally {
      setWorking(null);
    }
  };

  const remove = async (assetId) => {
    if (!projectId || !assetId) return;
    const ok = await confirm({
      title: "Delete this thumbnail?",
      description: "The generated image file will be permanently removed.",
      confirmLabel: "Delete",
      tone: "destructive",
    });
    if (!ok) return;
    setWorking(assetId);
    try {
      await api.delete(`/projects/${projectId}/assets/${assetId}`);
      await refresh();
      toast.success("Deleted");
    } catch (err) {
      toast.error("Delete failed", { description: formatApiError(err.response?.data?.detail) || err.message });
    } finally {
      setWorking(null);
    }
  };

  const copyPrompt = async (prompt) => {
    try {
      await navigator.clipboard.writeText(prompt || "");
      toast.success("Prompt copied");
    } catch { toast.error("Copy failed"); }
  };

  const Row = ({ label, value }) => (
    <div className="grid grid-cols-[110px_1fr] gap-3 text-sm">
      <span className="font-mono text-[10px] uppercase tracking-widest text-zinc-500 pt-0.5">{label}</span>
      <span className="text-zinc-200 leading-relaxed">{value}</span>
    </div>
  );

  if (metaLoading) {
    return (
      <div className="border border-zinc-800 border-dashed p-10 text-center rounded-sm">
        <Loader2 size={18} className="animate-spin text-[#00E5FF] mx-auto mb-3" />
        <p className="text-sm text-zinc-400">Loading thumbnail settings…</p>
      </div>
    );
  }

  if (metaError) {
    return (
      <div className="border border-[#FF3366]/30 bg-[#FF3366]/5 p-6 rounded-sm text-center space-y-3">
        <p className="text-sm text-[#FF3366]">Could not load thumbnail settings: {metaError}</p>
        <button
          onClick={loadMeta}
          className="inline-flex items-center gap-2 font-mono text-[10px] uppercase tracking-widest border border-zinc-700 hover:border-[#00E5FF] text-zinc-300 hover:text-[#00E5FF] px-3 py-2 rounded-sm transition-colors"
        >
          Retry
        </button>
      </div>
    );
  }

  if (briefs.length === 0) {
    return (
      <div className="border border-zinc-800 border-dashed p-10 text-center rounded-sm">
        {autoGeneratingConcepts ? (
          <div className="flex flex-col items-center gap-3">
            <Loader2 size={18} className="animate-spin text-[#00E5FF]" />
            <p className="text-sm text-zinc-400">Generating thumbnail concepts from metadata…</p>
          </div>
        ) : (
          <p className="text-sm text-zinc-400">No thumbnail briefs yet. Generate metadata first, then create thumbnail concepts.</p>
        )}
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div className="font-mono text-[11px] text-zinc-500">
          {autoGeneratingConcepts ? "Generating thumbnail concepts…" : `${briefs.length} concepts · ${meta.mock ? "mock images" : "Gemini Nano Banana"}`}
        </div>
        <div className="flex items-center gap-3">
          {canEdit && (
            <label className="flex items-center gap-2 text-xs text-zinc-400 cursor-pointer">
              <input
                type="checkbox"
                checked={autoSelectFirst}
                onChange={(e) => setAutoSelectFirst(e.target.checked)}
                className="h-3 w-3 accent-[#00E5FF] bg-[#0A0A0A] border-zinc-700 rounded"
              />
              Auto-select first concept
            </label>
          )}
          <div className="flex items-center gap-2">
          {meta.mock ? (
            <span
              data-testid="thumb-mock-badge"
              className="font-mono text-[10px] uppercase tracking-widest text-[#FFB020] border border-[#FFB020]/30 bg-[#FFB020]/10 px-2 py-0.5 rounded-sm"
            >
              Mock image
            </span>
          ) : (
            <span className="font-mono text-[10px] uppercase tracking-widest text-[#00FF66] border border-[#00FF66]/30 bg-[#00FF66]/10 px-2 py-0.5 rounded-sm">
              Generated · Gemini
            </span>
          )}
          </div>
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
        {briefs.map((a, i) => {
          const b = a.brief;
          const gens = generatedByBrief(a.id);
          const isGen = generating === a.id;
          return (
            <div
              key={a.id}
              data-testid={`thumb-concept-${a.id}`}
              className="border border-zinc-800 bg-[#121212] rounded-sm overflow-hidden flex flex-col"
            >
              <div className="px-5 py-4 border-b border-zinc-800 bg-[#0A0A0A]">
                <div className="font-mono text-[10px] uppercase tracking-widest text-zinc-500 mb-2">
                  Concept · {String(i + 1).padStart(2, "0")}
                </div>
                <div className="text-2xl font-bold tracking-tight text-white">{b?.thumbnail_title_text || "Untitled concept"}</div>
              </div>

              <div className="p-5 space-y-3">
                <Row label="Composition" value={b?.visual_composition || "—"} />
                <Row label="Emotion" value={b?.emotion_angle || "—"} />
                <Row label="Focal point" value={b?.subject_focal_point || "—"} />
                <Row label="Colour" value={b?.colour_direction || "—"} />
                <Row label="Click trigger" value={b?.click_trigger || "—"} />
              </div>

              {/* Generated variants */}
              {gens.length > 0 && (
                <div className="border-t border-zinc-800 p-4 space-y-3">
                  <div className="font-mono text-[10px] uppercase tracking-widest text-zinc-500">
                    {gens.length} generated image{gens.length > 1 ? "s" : ""}
                  </div>
                  <div className="grid grid-cols-2 gap-2">
                    {gens.map((g) => {
                      const isSelected = g.status === "selected" || selectedThumbnailId === g.id;
                      const isRejected = g.status === "rejected";
                      const isWorking = working === g.id;
                      return (
                        <div
                          key={g.id}
                          data-testid={`thumb-gen-${g.id}`}
                          className={`relative border rounded-sm overflow-hidden group ${
                            isSelected
                              ? "border-[#00FF66] shadow-[0_0_0_1px_#00FF66]"
                              : isRejected
                              ? "border-[#FF3366]/40 opacity-50"
                              : "border-zinc-800"
                          }`}
                        >
                          <div className="aspect-video bg-[#1A1A1A]">
                            {g.preview_url && (
                              <img
                                src={g.preview_url}
                                alt={g.name}
                                className="w-full h-full object-cover"
                                loading="lazy"
                              />
                            )}
                          </div>
                          <span
                            className="absolute top-1.5 left-1.5 font-mono text-[9px] uppercase tracking-widest px-1.5 py-0.5 rounded-sm border"
                            style={
                              isSelected
                                ? { color: "#00FF66", background: "rgba(0,255,102,0.12)", borderColor: "#00FF66" }
                                : isRejected
                                ? { color: "#FF3366", background: "rgba(255,51,102,0.1)", borderColor: "#FF3366" }
                                : g.mock
                                ? { color: "#FFB020", background: "rgba(255,176,32,0.1)", borderColor: "#FFB020" }
                                : { color: "#00E5FF", background: "rgba(0,229,255,0.1)", borderColor: "#00E5FF" }
                            }
                          >
                            {isSelected ? "Selected" : isRejected ? "Rejected" : g.mock ? "Mock" : "Generated"}
                          </span>
                          {canEdit && (
                            <div className="absolute inset-0 bg-black/0 group-hover:bg-black/70 opacity-0 group-hover:opacity-100 flex flex-wrap items-center justify-center gap-1 p-2 transition-opacity">
                              {!isSelected && (
                                <button
                                  data-testid={`thumb-select-${g.id}`}
                                  onClick={() => select(g.id)}
                                  disabled={isWorking}
                                  className="flex items-center gap-1 font-mono text-[9px] uppercase tracking-widest bg-[#00FF66] text-black px-2 py-1 rounded-sm hover:bg-[#33FF80] disabled:opacity-50"
                                >
                                  {isWorking ? <Loader2 size={10} className="animate-spin" /> : <Star size={10} strokeWidth={2} />}
                                  Select
                                </button>
                              )}
                              {!isRejected && (
                                <button
                                  data-testid={`thumb-reject-${g.id}`}
                                  onClick={() => reject(g.id)}
                                  disabled={isWorking}
                                  className="flex items-center gap-1 font-mono text-[9px] uppercase tracking-widest border border-[#FF3366]/40 text-[#FF3366] hover:bg-[#FF3366]/10 px-2 py-1 rounded-sm disabled:opacity-50"
                                >
                                  <XIcon size={10} strokeWidth={2} />
                                  Reject
                                </button>
                              )}
                              <a
                                data-testid={`thumb-open-${g.id}`}
                                href={g.preview_url}
                                target="_blank"
                                rel="noreferrer"
                                className="flex items-center gap-1 font-mono text-[9px] uppercase tracking-widest border border-zinc-600 text-zinc-200 hover:border-[#00E5FF] hover:text-[#00E5FF] px-2 py-1 rounded-sm"
                              >
                                <ExternalLink size={10} strokeWidth={1.8} /> Open
                              </a>
                              <button
                                data-testid={`thumb-copy-prompt-${g.id}`}
                                onClick={() => copyPrompt(g.prompt)}
                                className="flex items-center gap-1 font-mono text-[9px] uppercase tracking-widest border border-zinc-600 text-zinc-200 hover:border-[#00E5FF] hover:text-[#00E5FF] px-2 py-1 rounded-sm"
                              >
                                <Copy size={10} strokeWidth={1.5} /> Prompt
                              </button>
                              <button
                                data-testid={`thumb-delete-${g.id}`}
                                onClick={() => remove(g.id)}
                                disabled={isWorking}
                                className="flex items-center gap-1 font-mono text-[9px] uppercase tracking-widest border border-zinc-600 text-zinc-400 hover:border-[#FF3366] hover:text-[#FF3366] px-2 py-1 rounded-sm disabled:opacity-50"
                              >
                                Delete
                              </button>
                            </div>
                          )}
                        </div>
                      );
                    })}
                  </div>
                </div>
              )}

              {canEdit && (
                <div className="border-t border-zinc-800 px-4 py-3 flex items-center gap-2">
                  <button
                    data-testid={`generate-image-btn-${a.id}`}
                    onClick={() => generate(a.id, 1)}
                    disabled={isGen}
                    className="flex-1 flex items-center justify-center gap-1.5 font-mono text-[10px] uppercase tracking-widest bg-[#00E5FF] text-black hover:bg-[#33EFFF] px-2 py-2 rounded-sm disabled:opacity-60 transition-colors"
                  >
                    {isGen ? <Loader2 size={11} className="animate-spin" /> : <ImageIcon size={11} strokeWidth={1.8} />}
                    {gens.length > 0 ? "Generate Another" : "Generate Image"}
                  </button>
                  <button
                    data-testid={`generate-variants-btn-${a.id}`}
                    onClick={() => generate(a.id, 3)}
                    disabled={isGen}
                    className="flex items-center justify-center gap-1.5 font-mono text-[10px] uppercase tracking-widest border border-[#7B61FF]/40 text-[#7B61FF] hover:bg-[#7B61FF]/10 px-2 py-2 rounded-sm disabled:opacity-60 transition-colors"
                  >
                    {isGen ? <Loader2 size={11} className="animate-spin" /> : <Wand2 size={11} strokeWidth={1.8} />}
                    3 Variants
                  </button>
                </div>
              )}

              {/* Brief image-prompt display */}
              <div className="border-t border-zinc-800 px-5 py-3">
                <div className="font-mono text-[10px] uppercase tracking-widest text-zinc-500 mb-1">Base image prompt</div>
                <div className="font-mono text-xs text-[#7B61FF] leading-relaxed line-clamp-2">{b?.image_prompt || "No prompt available"}</div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
