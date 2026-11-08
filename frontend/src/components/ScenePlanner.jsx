import React, { useState, useEffect } from "react";
import { toast } from "sonner";
import { Sparkles, Loader2, Download, Search, X as XIcon, Wand2, CreditCard, Zap, Info, Save, Film } from "lucide-react";
import { api, formatApiError, API } from "../lib/api";
import { formatDuration } from "../lib/format";
import StockAssetModal from "./StockAssetModal";
import { useConfirm } from "./ConfirmDialog";
import BillingBar from "./BillingBar";
import { useNavigate } from "react-router-dom";
import { createDodoCheckout, DODO_PRODUCT_IDS } from "../lib/dodo";
import { beginAssetAttachment, finishAssetAttachment, pendingAssetAttachment } from "../lib/assetAttachmentOperation";

function InfoTip({ text }) {
  const [show, setShow] = useState(false);
  return (
    <span
      className="relative inline-flex"
      onMouseEnter={() => setShow(true)}
      onMouseLeave={() => setShow(false)}
    >
      <span className="inline-flex items-center justify-center w-4 h-4 rounded-full bg-blue-500/20 border border-blue-400/40 text-blue-400 text-[10px] font-bold cursor-help">ⓘ</span>
      {show && (
        <span className="absolute left-1/2 -translate-x-1/2 bottom-6 bg-slate-800 text-xs text-slate-100 px-3 py-2 rounded-md shadow-lg whitespace-nowrap z-50 border border-slate-700">
          {text}
        </span>
      )}
    </span>
  );
}

function HumanParamsSidebar({ projectId, project, onChange, canEdit }) {
  const [audience, setAudience] = useState(project?.audience || "");
  const [goal, setGoal] = useState(project?.monetisation_intent || "Grow channel to 50k subs");
  const [cta, setCta] = useState(project?.cta_goal || "Subscribe");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setAudience(project?.audience || "");
    setGoal(project?.monetisation_intent || "Grow channel to 50k subs");
    setCta(project?.cta_goal || "Subscribe");
  }, [project?.audience, project?.monetisation_intent, project?.cta_goal]);

  const save = async () => {
    setSaving(true);
    try {
      const { data } = await api.patch(`/projects/${projectId}`, {
        audience,
        monetisation_intent: goal,
        cta_goal: cta,
      });
      toast.success("Saved");
      onChange(data);
    } catch (e) {
      toast.error("Save failed", { description: formatApiError(e.response?.data?.detail) || e.message });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="bg-[#1c2333] border border-[rgba(59,130,246,0.15)] rounded-lg p-5 space-y-5" style={{ boxShadow: "0 0 20px rgba(59,130,246,0.05)" }}>
      <h3 className="font-mono text-[11px] uppercase tracking-widest text-slate-400">Project Goals</h3>

      <div className="space-y-1.5">
        <label className="flex items-center gap-2 text-sm font-medium text-slate-200">
          Who's watching? <InfoTip text="Who will watch this? This shapes voice + examples" />
        </label>
        <input
          data-testid="field-audience-human"
          value={audience}
          onChange={(e) => setAudience(e.target.value)}
          placeholder="e.g. Gen Z tech hobbyists, busy pros"
          disabled={!canEdit}
          className="w-full bg-[#0f141f] border border-slate-700 rounded-md px-3 py-2 text-sm text-slate-200 placeholder:text-slate-500 focus:border-blue-500 disabled:opacity-60"
        />
      </div>

      <div className="space-y-1.5">
        <label className="flex items-center gap-2 text-sm font-medium text-slate-200">
          What's the goal? <InfoTip text="What do you want this video to do?" />
        </label>
        <select
          data-testid="field-goal-human"
          value={goal}
          onChange={(e) => setGoal(e.target.value)}
          disabled={!canEdit}
          className="w-full bg-[#0f141f] border border-slate-700 rounded-md px-3 py-2 text-sm text-slate-200 focus:border-blue-500 disabled:opacity-60"
        >
          <option>Grow channel to 50k subs</option>
          <option>Sell my product</option>
          <option>Build authority</option>
          <option>Drive affiliate sales</option>
          <option>Educate audience</option>
          <option>Generate leads</option>
        </select>
      </div>

      <div className="space-y-1.5">
        <label className="flex items-center gap-2 text-sm font-medium text-slate-200">
          What should they do? <InfoTip text="Main action you want viewer to take" />
        </label>
        <select
          data-testid="field-cta-human"
          value={cta}
          onChange={(e) => setCta(e.target.value)}
          disabled={!canEdit}
          className="w-full bg-[#0f141f] border border-slate-700 rounded-md px-3 py-2 text-sm text-slate-200 focus:border-blue-500 disabled:opacity-60"
        >
          <option>Visit website</option>
          <option>Subscribe</option>
          <option>Buy product</option>
          <option>Join newsletter</option>
          <option>Book a call</option>
        </select>
      </div>

      {canEdit && (
        <button
          data-testid="save-human-params"
          onClick={save}
          disabled={saving}
          className="w-full flex items-center justify-center gap-2 bg-blue-600 text-white text-sm font-semibold py-2 rounded-md hover:bg-blue-500 disabled:opacity-50"
        >
          {saving ? <Loader2 size={14} className="animate-spin" /> : <Save size={14} />} Save
        </button>
      )}
    </div>
  );
}

export default function ScenePlanner({ projectId, scenes, canEdit, onChange, hasScript, attachedAssets = [], project }) {
  const [generating, setGenerating] = useState(false);
  const [autoAttaching, setAutoAttaching] = useState(false);
  const [pendingAttachment, setPendingAttachment] = useState(null);
  const [activeScene, setActiveScene] = useState(null);
  const [previewAsset, setPreviewAsset] = useState(null);
  const [credits, setCredits] = useState(null);
  const [exhaustedInfo, setExhaustedInfo] = useState(null);
  const [showPricingModal, setShowPricingModal] = useState(false);
  const [fullRenderLoading, setFullRenderLoading] = useState(false);
  const [innerProject, setInnerProject] = useState(project || null);
  const confirm = useConfirm();
  const navigate = useNavigate();

  useEffect(() => {
    try { setPendingAttachment(pendingAssetAttachment(projectId)); }
    catch (err) { toast.error("Asset retry needs reconciliation", { description: err.message }); }
  }, [projectId]);

  useEffect(() => {
    if (project) setInnerProject(project);
  }, [project]);

  useEffect(() => {
    if (!project && projectId) {
      (async () => {
        try {
          const { data } = await api.get(`/projects/${projectId}`);
          setInnerProject(data.project);
        } catch {}
      })();
    }
  }, [project, projectId]);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const { data } = await api.get("/billing/credits");
        if (alive) setCredits(data);
      } catch {}
    })();
    return () => { alive = false; };
  }, [attachedAssets]);

  const handleExhausted = (err) => {
    const detail = err?.response?.data?.detail;
    const isExhausted = err?.response?.status === 402 || (detail && detail.code === "credits_exhausted");
    if (isExhausted) {
      const info = typeof detail === "object" ? detail : { code: "credits_exhausted", reset_at: credits?.reset_at, plan: credits?.plan || "free", remaining: credits?.remaining ?? 0 };
      setExhaustedInfo(info);
      setShowPricingModal(true);
      return true;
    }
    return false;
  };

  const generate = async () => {
    if (credits && !credits.operator_unmetered && (credits.remaining ?? credits.credits) <= 0) {
      setExhaustedInfo({ reset_at: credits.reset_at || credits.current_period_end, plan: credits.plan || "free" });
      setShowPricingModal(true);
      return;
    }
    setGenerating(true);
    const preserve = scenes.length > 0;
    toast.loading(preserve ? "Regenerating scene content (keeping count)…" : "Generating scene plan…", { id: "gen-scenes" });
    try {
      const { data } = await api.post(`/projects/${projectId}/generate-scenes`, preserve ? { preserveSceneCount: true } : {});
      onChange(data);
      if (data.project) setInnerProject(data.project);
      toast.success(preserve ? "Scenes regenerated (count preserved)" : "Scenes generated", { id: "gen-scenes" });
    } catch (err) {
      if (!handleExhausted(err)) toast.error("Generation failed", { id: "gen-scenes", description: formatApiError(err.response?.data?.detail) || err.message });
      else toast.error("Credits exhausted", { id: "gen-scenes", description: `0 left, resets ${(() => { try { return new Date(err.response?.data?.detail?.reset_at || credits?.reset_at).toLocaleDateString(); } catch { return err.response?.data?.detail?.reset_at || ""; }})()}` });
    } finally {
      setGenerating(false);
    }
  };

  const exportCsv = () => {
    window.open(`${API}/projects/${projectId}/export/scenes.csv`, "_blank");
  };

  const fullRender = async () => {
    setFullRenderLoading(true);
    try {
      await api.post(`/render/full/${projectId}`);
      toast.success("Render queued");
    } catch (e) {
      // fallback to /projects/{id}/render/start or /render/full
      try {
        await api.post(`/projects/${projectId}/render/full`);
        toast.success("Render queued");
      } catch (e2) {
        try {
          await api.post(`/projects/${projectId}/render/start`, {});
          toast.success("Render queued");
        } catch (e3) {
          toast.error("Render failed", { description: formatApiError(e3.response?.data?.detail) || e3.message });
        }
      }
    } finally {
      setFullRenderLoading(false);
    }
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
      (a) => a.scene_id === sceneId && ["stock_video", "stock_image", "client_video", "client_image", "before_after", "brand_image"].includes(a.asset_type)
    );

  const uploadClientAsset = async (scene, file) => {
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    form.append("scene_id", scene.id);
    form.append("asset_type", file.type.startsWith("video/") ? "client_video" : "client_image");
    try {
      await api.post(`/projects/${projectId}/assets/upload`, form, { headers: { "Content-Type": "multipart/form-data" } });
      const { data } = await api.get(`/projects/${projectId}`);
      onChange(data);
      toast.success(`Client asset assigned to scene ${scene.scene_number}`);
    } catch (err) {
      toast.error("Client upload failed", { description: formatApiError(err.response?.data?.detail) || err.message });
    }
  };

  const uploadBrandAsset = async (file) => {
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    form.append("asset_type", file.type.startsWith("image/") ? "brand_logo" : "brand_image");
    try {
      await api.post(`/projects/${projectId}/assets/upload`, form, { headers: { "Content-Type": "multipart/form-data" } });
      const { data } = await api.get(`/projects/${projectId}`);
      onChange(data);
      toast.success("Brand asset uploaded with client provenance");
    } catch (err) {
      toast.error("Brand upload failed", { description: formatApiError(err.response?.data?.detail) || err.message });
    }
  };

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
    let pending;
    try { pending = pendingAssetAttachment(projectId); }
    catch (err) {
      toast.error("Asset retry needs reconciliation", { description: err.message });
      return;
    }
    // A paid operation can need replay at zero balance after its response was lost.
    if (!pending && credits && !credits.operator_unmetered && (credits.remaining ?? credits.credits) <= 0) {
      setExhaustedInfo({ reset_at: credits.reset_at || credits.current_period_end, plan: credits.plan || "free" });
      setShowPricingModal(true);
      return;
    }
    let replaceExisting = pending?.payload.replace_existing || false;
    if (!pending && scenesWithAssets > 0) {
      const ok = await confirm({
        title: "Replace existing scene assets?",
        description: `${scenesWithAssets} scenes already have assets. Choose Replace to rebuild everything, or Cancel to only fill the ${scenesWithoutAssets} empty scenes.`,
        confirmLabel: "Replace all",
        cancelLabel: scenesWithoutAssets > 0 ? `Fill ${scenesWithoutAssets} empty only` : "Cancel",
        tone: "warning",
      });
      replaceExisting = !!ok;
      if (!ok && scenesWithoutAssets === 0) return;
    }
    setAutoAttaching(true);
    const toastId = toast.loading(
      `Auto-attaching to ${replaceExisting ? "all" : scenesWithoutAssets} scenes…`,
      { id: "auto-attach" },
    );
    try {
      const operation = pending || beginAssetAttachment(projectId, replaceExisting);
      setPendingAttachment(operation);
      const { data } = await api.post(`/projects/${projectId}/auto-attach-assets`, operation.payload);
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
      try { const { data } = await api.get("/billing/credits"); setCredits(data); } catch {}
      // Keep the identity if the UI refresh failed or the 2xx body was resumable.
      if (finishAssetAttachment(operation, data)) setPendingAttachment(null);
    } catch (err) {
      if (!handleExhausted(err)) toast.error("Auto-attach failed", { id: toastId, description: formatApiError(err.response?.data?.detail) || err.message });
      else {
        const resetLabel = (() => { try { return new Date(err.response?.data?.detail?.reset_at || credits?.reset_at).toLocaleDateString(); } catch { return err.response?.data?.detail?.reset_at || ""; }})();
        toast.error("Credits exhausted", { id: toastId, description: `0 left, resets ${resetLabel}` });
      }
    } finally {
      setAutoAttaching(false);
    }
  };

  const handleBuyExtra = async (productId) => {
    try {
      const { checkout_url } = await createDodoCheckout(productId, 1);
      toast.success("Redirecting to Dodo checkout");
      window.location.href = checkout_url;
    } catch (e) {
      const msg = e?.response?.data?.detail || e?.message || String(e);
      toast.error("Checkout failed", { description: msg });
    }
  };

  const exhausted = credits && !credits.operator_unmetered && ((credits.remaining ?? credits.credits ?? 1) <= 0);
  const resetLabel = (() => { const r = credits?.reset_at || credits?.current_period_end; if (!r) return "next cycle"; try { return new Date(r).toLocaleDateString(); } catch { return String(r).slice(0,10); } })();

  if (!scenes || scenes.length === 0) {
    return (
      <div className="grid grid-cols-1 lg:grid-cols-[320px_1fr] gap-6">
        <HumanParamsSidebar projectId={projectId} project={innerProject} onChange={onChange} canEdit={canEdit} />
        <div className="border border-[rgba(59,130,246,0.15)] border-dashed p-10 text-center rounded-lg bg-[#1c2333]">
          <p className="text-sm text-slate-400 mb-4">
            {hasScript ? "No scene plan yet. Generate one from the script." : "Generate a script first, then break it into scenes."}
          </p>
          {canEdit && hasScript && (
            <button
              data-testid="generate-scenes-btn"
              onClick={generate}
              disabled={generating || exhausted}
              title={exhausted ? `0 left, resets ${resetLabel}` : undefined}
              className={`inline-flex items-center gap-2 font-semibold text-sm px-4 py-2 rounded-md transition-colors disabled:opacity-60 ${exhausted ? "bg-slate-800 text-slate-500 cursor-not-allowed" : "bg-blue-600 text-white hover:bg-blue-500"}`}
            >
              {generating ? <Loader2 size={14} className="animate-spin" /> : <Sparkles size={14} strokeWidth={2} />}
              {exhausted ? `0 left, resets ${resetLabel}` : generating ? "Generating…" : "Generate Scenes"}
            </button>
          )}
          {exhausted && <div className="mt-3 font-mono text-[11px] text-red-400">Credits exhausted — <button onClick={() => setShowPricingModal(true)} className="underline">Buy extra or Upgrade</button></div>}
        </div>
      </div>
    );
  }

  return (
    <div className="grid grid-cols-1 lg:grid-cols-[320px_1fr] gap-6">
      <div className="space-y-4 self-start sticky top-20">
        <HumanParamsSidebar projectId={projectId} project={innerProject} onChange={onChange} canEdit={canEdit} />
        {canEdit && project?.production_mode !== "commercial_ad" && (
          <button
            data-testid="full-render-btn"
            onClick={fullRender}
            disabled={fullRenderLoading}
            className="w-full flex items-center justify-center gap-2 bg-gradient-to-r from-blue-600 to-indigo-600 text-white font-bold text-sm px-4 py-3 rounded-md hover:from-blue-500 hover:to-indigo-500 disabled:opacity-50 shadow-lg"
          >
            {fullRenderLoading ? <Loader2 size={14} className="animate-spin" /> : <Film size={16} />} FULL RENDER - START TO FINISH
          </button>
        )}
      </div>

      <div className="space-y-4 min-w-0">
        <BillingBar />
        <div className="flex items-center justify-between">
          <div className="font-mono text-[11px] text-slate-500" title={`Locked: ceil(${innerProject?.target_duration || project?.target_duration || 0}s / 30s)`}>
            {(() => {
              const total = innerProject?.target_duration || project?.target_duration || 0;
              const expected = Math.ceil(total / 30);
              const actual = scenes.length;
              const totalStr = formatDuration(scenes[scenes.length - 1]?.end_time || total);
              const mismatch = actual !== expected && actual > 0;
              return (
                <span className={mismatch ? "text-amber-400" : ""}>
                  Total: {total}s = {expected} × 30s · {actual} scenes · {totalStr} {mismatch ? "⚠ mismatch" : ""}
                </span>
              );
            })()}
          </div>
          <div className="flex items-center gap-2">
            {canEdit && project?.production_mode !== "commercial_ad" && (
              <button
                data-testid="auto-attach-btn"
                onClick={autoAttach}
                disabled={autoAttaching || (exhausted && !pendingAttachment)}
                title={exhausted && !pendingAttachment ? `0 left, resets ${resetLabel}` : undefined}
                className={`flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-widest px-2.5 py-1 rounded-md transition-colors disabled:opacity-60 ${exhausted && !pendingAttachment ? "bg-slate-800 text-slate-500 cursor-not-allowed" : "text-white bg-emerald-600 hover:bg-emerald-500"}`}
              >
                {autoAttaching ? <Loader2 size={12} className="animate-spin" /> : <Wand2 size={12} strokeWidth={1.8} />}
                {autoAttaching ? "Auto-attaching…" : pendingAttachment ? "Retry asset update" : exhausted ? `0 left, resets ${resetLabel}` : "Auto-attach Assets"}
              </button>
            )}
            {canEdit && project?.production_mode === "commercial_ad" && (
              <label className="font-mono text-[10px] uppercase tracking-widest text-[#00E5FF] border border-[#00E5FF]/40 px-2 py-1 rounded-md cursor-pointer">
                Upload logo / brand asset
                <input data-testid="upload-brand-asset" type="file" accept="image/*" className="hidden" onChange={(e) => uploadBrandAsset(e.target.files?.[0])} />
              </label>
            )}
            <button
              data-testid="export-scenes-csv"
              onClick={exportCsv}
              className="flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-widest text-slate-400 hover:text-blue-400 border border-[rgba(59,130,246,0.15)] hover:border-blue-400 px-2 py-1 rounded-md transition-colors"
            >
              <Download size={12} strokeWidth={1.5} /> CSV
            </button>
            {canEdit && (
              <button
                data-testid="regenerate-scenes-btn"
                onClick={generate}
                disabled={generating || exhausted}
                title={exhausted ? `0 left, resets ${resetLabel}` : undefined}
                className={`flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-widest px-2 py-1 rounded-md border transition-colors disabled:opacity-50 ${exhausted ? "border-slate-700 text-slate-600 cursor-not-allowed" : "text-slate-400 hover:text-blue-400 border-[rgba(59,130,246,0.15)] hover:border-blue-400"}`}
              >
                {generating ? <Loader2 size={12} className="animate-spin" /> : <Sparkles size={12} strokeWidth={1.5} />}
                {exhausted ? `0 left, resets ${resetLabel}` : "Regenerate"}
              </button>
            )}
          </div>
        </div>

        <div className="border border-[rgba(59,130,246,0.15)] bg-[#1c2333] rounded-lg overflow-hidden">
          <table className="w-full text-sm">
            <thead className="border-b border-[rgba(59,130,246,0.15)] bg-[#0f141f]">
              <tr className="font-mono text-[10px] text-slate-500 uppercase tracking-widest">
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
                  <tr key={s.id} className="border-b border-slate-800 hover:bg-[#1e293b] transition-colors align-top">
                    <td className="px-4 py-3 font-mono text-blue-400 text-xs">{String(s.scene_number).padStart(2, "0")}</td>
                    <td className="px-4 py-3 font-mono text-xs text-slate-400">
                      {formatDuration(s.start_time || 0)} → {formatDuration(s.end_time || 0)}
                    </td>
                    <td className="px-4 py-3 text-slate-200 leading-relaxed">
                      {s.narration_text}
                      {(() => {
                        const v = voiceoverForScene(s.id);
                        if (!v) return null;
                        return (
                          <div data-testid={`scene-voiceover-${s.id}`} className="mt-2 flex items-center gap-2">
                            <span
                              className="font-mono text-[9px] uppercase tracking-widest px-1.5 py-0.5 rounded-md border"
                              style={{
                                color: "#f472b6",
                                background: "rgba(244,114,182,0.1)",
                                borderColor: "rgba(244,114,182,0.4)",
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
                      <div className="text-slate-300 mb-1">{s.visual_direction}</div>
                      <div className="font-mono text-[10px] uppercase tracking-widest text-amber-400">
                        {(s.asset_type || "").replace(/_/g, " ")}
                      </div>
                      {s.search_terms?.length > 0 && (
                        <div className="mt-2 flex flex-wrap gap-1">
                          {s.search_terms.map((t) => (
                            <span key={`term-${s.id}-${t}`} className="font-mono text-[10px] px-1.5 py-0.5 border border-slate-700 text-slate-400 rounded-md">
                              {t}
                            </span>
                          ))}
                        </div>
                      )}
                    </td>
                    <td className="px-4 py-3 space-y-2">
                      <div className="text-slate-300 italic">"{s.caption_text}"</div>
                      {sceneAssets.length > 0 && (
                        <>
                          <div className="grid grid-cols-3 gap-2 max-w-[420px]">
                            {sceneAssets.slice(0,10).map((a, i) => (
                              <div key={a.id} data-testid={`scene-asset-${a.id}`} className="relative aspect-video bg-black border border-slate-700 group overflow-hidden rounded-md cursor-pointer" title={`${a.name} · ${a.attribution_name || ""} — click to preview`} onClick={() => setPreviewAsset(a)}>
                                {a.preview_url ? <img src={a.preview_url} alt="" className="w-full h-full object-cover" loading="lazy" referrerPolicy="no-referrer" onError={(e)=>{e.currentTarget.src='/fallback-thumb.png'; e.currentTarget.onerror=null;}} /> : <div className="w-full h-full bg-[#0f141f]" />}
                                <span className="absolute bottom-0 left-0 text-[7px] bg-black/70 px-1 text-white">{i*3}s</span>
                                {a.source && <span className="absolute top-0 left-0 text-[6px] bg-blue-500 text-white px-0.5">{a.source[0].toUpperCase()}</span>}
                                {canEdit && (
                                  <button data-testid={`detach-asset-${a.id}`} onClick={(e) => { e.stopPropagation(); detach(a); }} className="absolute top-1 right-1 w-6 h-6 bg-black/80 hover:bg-red-500 rounded-full flex items-center justify-center text-white hover:text-white opacity-0 group-hover:opacity-100 transition-opacity">
                                    <XIcon size={14} strokeWidth={2} />
                                  </button>
                                )}
                              </div>
                            ))}
                          </div>
                          <div className="text-[9px] text-slate-500 mt-1">{credits?.remaining ?? 30} credits · {sceneAssets.length} clips · {Math.round((s.end_time - s.start_time) || s.duration || 30)}s · 3s each</div>
                        </>
                      )}
                      {canEdit && (
                        <div className="flex flex-wrap gap-2">
                          {project?.production_mode === "commercial_ad" ? (
                            <label className="inline-flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-widest text-[#00E5FF] border border-[#00E5FF]/40 px-2 py-1 rounded-md cursor-pointer">
                              Upload client asset
                              <input data-testid={`upload-client-asset-${s.id}`} type="file" accept="image/*,video/*" className="hidden" onChange={(e) => uploadClientAsset(s, e.target.files?.[0])} />
                            </label>
                          ) : (
                            <button data-testid={`find-assets-btn-${s.id}`} onClick={() => setActiveScene(s)} className="inline-flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-widest text-blue-400 hover:text-white border border-blue-500/30 hover:border-blue-400 px-2 py-1 rounded-md transition-colors">
                              <Search size={11} strokeWidth={1.5} /> {sceneAssets.length > 0 ? "Find more" : "Find Assets"}
                            </button>
                          )}
                        </div>
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

        {previewAsset && (
          <div data-testid="preview-modal" className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-sm p-4" onClick={() => setPreviewAsset(null)}>
            <div className="relative bg-[#1c2333] border border-slate-700 rounded-lg max-w-3xl w-full overflow-hidden" onClick={(e) => e.stopPropagation()}>
              <button onClick={() => setPreviewAsset(null)} className="absolute top-2 right-2 w-8 h-8 bg-black/60 hover:bg-black rounded-full flex items-center justify-center text-white z-10">
                <XIcon size={16} />
              </button>
              <div className="aspect-video bg-black">
                {previewAsset.preview_url?.match(/\.(mp4|mov|webm)/) || previewAsset.asset_type === "stock_video" ? (
                  <video src={previewAsset.preview_url} controls autoPlay muted loop className="w-full h-full object-contain" />
                ) : (
                  <img src={previewAsset.preview_url} alt={previewAsset.name} className="w-full h-full object-contain" />
                )}
              </div>
              <div className="p-4 space-y-2">
                <div className="text-sm text-white font-medium">{previewAsset.name}</div>
                <div className="text-xs text-slate-400">{previewAsset.attribution_name || previewAsset.source || ""} · {previewAsset.width}×{previewAsset.height}</div>
                <div className="flex gap-2 pt-2">
                  <a href={previewAsset.preview_url} target="_blank" rel="noreferrer" className="text-xs text-blue-400 hover:underline">Open original</a>
                  <span className="text-xs text-slate-600">|</span>
                  <button onClick={() => { setPreviewAsset(null); detach(previewAsset); }} className="text-xs text-red-400 hover:underline">Detach</button>
                </div>
              </div>
            </div>
          </div>
        )}

        {showPricingModal && (
          <div data-testid="credits-exhausted-modal" className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm p-4">
            <div className="bg-[#1c2333] border border-[rgba(59,130,246,0.15)] rounded-lg max-w-lg w-full p-6 space-y-4">
              <div className="flex items-center gap-2 text-amber-400 font-mono text-xs uppercase tracking-widest"><CreditCard size={14} /> Credits exhausted</div>
              <h3 className="text-lg font-semibold text-white">0 credits left</h3>
              <p className="text-sm text-slate-400">Resets {resetLabel} · Plan {(exhaustedInfo?.plan || credits?.plan || "free")} · {exhaustedInfo?.remaining ?? credits?.remaining ?? 0} / {exhaustedInfo?.quota ?? credits?.monthly ?? 30}</p>
              <div className="grid grid-cols-2 gap-3 pt-2">
                <button
                  data-testid="modal-buy-extra-100"
                  onClick={() => handleBuyExtra(DODO_PRODUCT_IDS.topup_100)}
                  className="flex flex-col items-center gap-1 bg-emerald-600 text-white font-mono text-xs uppercase tracking-widest px-3 py-3 rounded-md hover:bg-emerald-500"
                >
                  <span className="font-bold">Buy extra 100</span><span className="text-[11px]">$19 one-time</span>
                </button>
                <button
                  data-testid="modal-buy-extra-300"
                  onClick={() => handleBuyExtra(DODO_PRODUCT_IDS.topup_300)}
                  className="flex flex-col items-center gap-1 bg-blue-600 text-white font-mono text-xs uppercase tracking-widest px-3 py-3 rounded-md hover:bg-blue-500"
                >
                  <span className="font-bold">Buy extra 300</span><span className="text-[11px]">$49 one-time</span>
                </button>
              </div>
              <div className="flex gap-2 pt-2">
                <button data-testid="modal-upgrade" onClick={() => navigate("/pricing")} className="flex-1 flex items-center justify-center gap-1 bg-white text-slate-900 font-mono text-xs uppercase tracking-widest px-3 py-2 rounded-md hover:bg-slate-200"><Zap size={12} /> Upgrade plan</button>
                <button data-testid="modal-close" onClick={() => setShowPricingModal(false)} className="px-3 py-2 font-mono text-xs text-slate-400 hover:text-white border border-slate-700 rounded-md">Close</button>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
