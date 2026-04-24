import React, { useState } from "react";
import { toast } from "sonner";
import { Share2, Copy, RefreshCcw, EyeOff, Loader2, ExternalLink, Pencil, Check, X } from "lucide-react";
import { api, formatApiError } from "../lib/api";
import { relativeTime } from "../lib/format";
import { useConfirm } from "./ConfirmDialog";

export default function SharePanel({ projectId, share, projectStatus, canEdit, onChange }) {
  const shareable = ["METADATA_GENERATED", "ASSETS_READY", "READY_TO_RENDER", "COMPLETED"].includes(projectStatus);
  const [busy, setBusy] = useState(false);
  const [editingTitle, setEditingTitle] = useState(false);
  const [titleDraft, setTitleDraft] = useState(share?.title_override || "");
  const confirm = useConfirm();

  const origin = typeof window !== "undefined" ? window.location.origin : "";
  const shareUrl = share?.token ? `${origin}/s/${share.token}` : null;

  const enable = async () => {
    setBusy(true);
    try {
      await api.post(`/projects/${projectId}/share`, {});
      toast.success("Share link enabled");
      await onChange?.();
    } catch (err) {
      toast.error("Could not enable", { description: formatApiError(err.response?.data?.detail) || err.message });
    } finally { setBusy(false); }
  };

  const disable = async () => {
    setBusy(true);
    try {
      await api.delete(`/projects/${projectId}/share`);
      toast.success("Share link disabled");
      await onChange?.();
    } catch (err) {
      toast.error("Could not disable", { description: formatApiError(err.response?.data?.detail) || err.message });
    } finally { setBusy(false); }
  };

  const regenerate = async () => {
    const ok = await confirm({
      title: "Regenerate share link?",
      description: "The old URL will stop working immediately. Anyone using the previous link will see a 404.",
      confirmLabel: "Regenerate token",
      tone: "warning",
    });
    if (!ok) return;
    setBusy(true);
    try {
      await api.post(`/projects/${projectId}/share/regenerate`);
      toast.success("New share token generated");
      await onChange?.();
    } catch (err) {
      toast.error("Regenerate failed", { description: formatApiError(err.response?.data?.detail) || err.message });
    } finally { setBusy(false); }
  };

  const saveTitle = async () => {
    setBusy(true);
    try {
      await api.patch(`/projects/${projectId}/share`, { title_override: titleDraft });
      toast.success("Public title updated");
      setEditingTitle(false);
      await onChange?.();
    } catch (err) {
      toast.error("Save failed", { description: formatApiError(err.response?.data?.detail) || err.message });
    } finally { setBusy(false); }
  };

  const copy = async () => {
    if (!shareUrl) return;
    try {
      await navigator.clipboard.writeText(shareUrl);
      toast.success("Link copied");
    } catch { toast.error("Copy failed"); }
  };

  return (
    <div className="border border-zinc-800 bg-[#121212] rounded-sm" data-testid="share-panel">
      <div className="px-5 py-3 border-b border-zinc-800 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Share2 size={14} strokeWidth={1.5} className="text-[#00E5FF]" />
          <span className="font-mono text-[10px] uppercase tracking-widest text-zinc-500">Public share link</span>
        </div>
        {share?.enabled && (
          <span className="font-mono text-[10px] uppercase tracking-widest text-[#00FF66] flex items-center gap-1.5">
            <span className="ff-dot" /> Live
          </span>
        )}
      </div>

      <div className="p-5 space-y-4">
        {!shareable ? (
          <div className="text-sm text-zinc-400">
            Sharing becomes available once the project reaches{" "}
            <span className="font-mono text-[#7B61FF]">METADATA_GENERATED</span> or later.
          </div>
        ) : !share?.enabled ? (
          <div className="space-y-3">
            <p className="text-sm text-zinc-300 leading-relaxed">
              Create a read-only public page at <span className="font-mono text-[#00E5FF]">/s/{"{token}"}</span> showing
              your title, description, tags, thumbnail briefs, and quality score. Private notes, costs, and provider
              details stay hidden.
            </p>
            {canEdit && (
              <button
                data-testid="share-enable-btn"
                onClick={enable}
                disabled={busy}
                className="inline-flex items-center gap-2 bg-[#00E5FF] text-black font-semibold text-sm px-4 py-2 rounded-sm hover:bg-[#33EFFF] disabled:opacity-60 transition-colors"
              >
                {busy ? <Loader2 size={14} className="animate-spin" /> : <Share2 size={14} strokeWidth={2} />}
                Enable share link
              </button>
            )}
          </div>
        ) : (
          <div className="space-y-4">
            {/* Link */}
            <div className="flex items-center gap-2">
              <input
                data-testid="share-url-input"
                readOnly
                value={shareUrl || ""}
                onClick={(e) => e.target.select()}
                className="flex-1 bg-[#0A0A0A] border border-zinc-800 px-3 py-2 text-xs font-mono text-[#00E5FF] rounded-sm focus:border-[#00E5FF]"
              />
              <button
                data-testid="share-copy-btn"
                onClick={copy}
                className="flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-widest text-zinc-400 hover:text-[#00E5FF] border border-zinc-800 hover:border-[#00E5FF] px-2 py-2 rounded-sm transition-colors"
              >
                <Copy size={12} strokeWidth={1.5} /> Copy
              </button>
              <a
                data-testid="share-open-btn"
                href={shareUrl || "#"}
                target="_blank"
                rel="noreferrer"
                className="flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-widest text-zinc-400 hover:text-[#00E5FF] border border-zinc-800 hover:border-[#00E5FF] px-2 py-2 rounded-sm transition-colors"
              >
                <ExternalLink size={12} strokeWidth={1.5} /> Open
              </a>
            </div>

            {/* Title override */}
            <div>
              <div className="font-mono text-[10px] uppercase tracking-widest text-zinc-500 mb-2">
                Public title override (optional)
              </div>
              {editingTitle ? (
                <div className="flex items-center gap-2">
                  <input
                    data-testid="share-title-input"
                    value={titleDraft}
                    onChange={(e) => setTitleDraft(e.target.value)}
                    placeholder="Leave empty to use selected title"
                    maxLength={200}
                    className="flex-1 bg-[#0A0A0A] border border-zinc-800 px-3 py-2 text-sm rounded-sm focus:border-[#00E5FF]"
                  />
                  <button
                    data-testid="share-title-save"
                    onClick={saveTitle}
                    disabled={busy}
                    className="bg-[#00E5FF] text-black px-2 py-2 rounded-sm hover:bg-[#33EFFF] transition-colors"
                  >
                    <Check size={14} strokeWidth={2} />
                  </button>
                  <button
                    data-testid="share-title-cancel"
                    onClick={() => { setEditingTitle(false); setTitleDraft(share?.title_override || ""); }}
                    className="border border-zinc-800 text-zinc-400 hover:text-white px-2 py-2 rounded-sm transition-colors"
                  >
                    <X size={14} strokeWidth={2} />
                  </button>
                </div>
              ) : (
                <button
                  data-testid="share-title-edit"
                  onClick={() => { setTitleDraft(share?.title_override || ""); setEditingTitle(true); }}
                  disabled={!canEdit}
                  className="flex items-center gap-2 text-sm text-zinc-300 hover:text-[#00E5FF] border border-dashed border-zinc-800 hover:border-[#00E5FF] px-3 py-2 w-full rounded-sm transition-colors disabled:opacity-50"
                >
                  <Pencil size={12} strokeWidth={1.5} />
                  {share?.title_override ? (
                    <span className="truncate">{share.title_override}</span>
                  ) : (
                    <span className="text-zinc-500">Using selected metadata title</span>
                  )}
                </button>
              )}
            </div>

            {/* Analytics */}
            <div className="grid grid-cols-2 gap-3 pt-3 border-t border-zinc-800">
              <div>
                <div className="font-mono text-[10px] uppercase tracking-widest text-zinc-500 mb-1">Views</div>
                <div className="font-mono text-2xl tabular-nums text-[#00E5FF]" data-testid="share-view-count">
                  {share.view_count}
                </div>
              </div>
              <div>
                <div className="font-mono text-[10px] uppercase tracking-widest text-zinc-500 mb-1">Last viewed</div>
                <div className="font-mono text-sm text-zinc-300">
                  {share.last_viewed_at ? relativeTime(share.last_viewed_at) : "never"}
                </div>
              </div>
            </div>

            {/* Actions */}
            {canEdit && (
              <div className="flex items-center gap-2 pt-3 border-t border-zinc-800">
                <button
                  data-testid="share-regenerate-btn"
                  onClick={regenerate}
                  disabled={busy}
                  className="flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-widest text-zinc-400 hover:text-[#FFB020] border border-zinc-800 hover:border-[#FFB020] px-3 py-2 rounded-sm transition-colors disabled:opacity-50"
                >
                  {busy ? <Loader2 size={12} className="animate-spin" /> : <RefreshCcw size={12} strokeWidth={1.5} />}
                  Regenerate token
                </button>
                <button
                  data-testid="share-disable-btn"
                  onClick={disable}
                  disabled={busy}
                  className="flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-widest text-zinc-400 hover:text-[#FF3366] border border-zinc-800 hover:border-[#FF3366] px-3 py-2 rounded-sm transition-colors disabled:opacity-50"
                >
                  <EyeOff size={12} strokeWidth={1.5} /> Disable
                </button>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
