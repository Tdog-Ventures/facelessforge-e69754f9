import React, { useEffect, useState } from "react";
import { toast } from "sonner";
import { Sparkles, Loader2, Save, Download } from "lucide-react";
import { api, formatApiError, API } from "../lib/api";
import CopyButton from "./CopyButton";

export default function MetadataPanel({ projectId, metadata, canEdit, onChange, hasScript }) {
  const [generating, setGenerating] = useState(false);
  const [saving, setSaving] = useState(false);
  const [draft, setDraft] = useState({
    selected_title: metadata?.selected_title || "",
    description: metadata?.description || "",
    tags: (metadata?.tags || []).join(", "),
    pinned_comment: metadata?.pinned_comment || "",
  });

  useEffect(() => {
    setDraft({
      selected_title: metadata?.selected_title || "",
      description: metadata?.description || "",
      tags: (metadata?.tags || []).join(", "),
      pinned_comment: metadata?.pinned_comment || "",
    });
  }, [metadata?.id]);

  const generate = async () => {
    setGenerating(true);
    toast.loading("Generating metadata package…", { id: "gen-meta" });
    try {
      const { data } = await api.post(`/projects/${projectId}/generate-metadata`);
      onChange(data);
      toast.success("Metadata generated", { id: "gen-meta" });
    } catch (err) {
      toast.error("Generation failed", { id: "gen-meta", description: formatApiError(err.response?.data?.detail) || err.message });
    } finally {
      setGenerating(false);
    }
  };

  const save = async () => {
    setSaving(true);
    try {
      const { data } = await api.patch(`/projects/${projectId}/metadata`, {
        selected_title: draft.selected_title,
        description: draft.description,
        tags: draft.tags.split(",").map((t) => t.trim()).filter(Boolean),
        pinned_comment: draft.pinned_comment,
      });
      onChange(data);
      toast.success("Metadata saved");
    } catch (err) {
      toast.error("Save failed", { description: formatApiError(err.response?.data?.detail) || err.message });
    } finally {
      setSaving(false);
    }
  };

  if (!metadata) {
    return (
      <div className="border border-zinc-800 border-dashed p-10 text-center rounded-sm">
        <p className="text-sm text-zinc-400 mb-4">
          {hasScript ? "No metadata package yet." : "Generate a script first."}
        </p>
        {canEdit && hasScript && (
          <button
            data-testid="generate-metadata-btn"
            onClick={generate}
            disabled={generating}
            className="inline-flex items-center gap-2 bg-[#00E5FF] text-black font-semibold text-sm px-4 py-2 rounded-sm hover:bg-[#33EFFF] disabled:opacity-60 transition-colors"
          >
            {generating ? <Loader2 size={14} className="animate-spin" /> : <Sparkles size={14} strokeWidth={2} />}
            Generate Metadata
          </button>
        )}
      </div>
    );
  }

  const exportJson = () => {
    window.open(`${API}/projects/${projectId}/export/metadata.json`, "_blank");
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div className="font-mono text-[11px] text-zinc-500">
          {metadata.title_options?.length || 0} title options · {metadata.tags?.length || 0} tags
        </div>
        <div className="flex items-center gap-2">
          <button
            data-testid="export-metadata-json"
            onClick={exportJson}
            className="flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-widest text-zinc-400 hover:text-[#00E5FF] border border-zinc-800 hover:border-[#00E5FF] px-2 py-1 rounded-sm transition-colors"
          >
            <Download size={12} strokeWidth={1.5} /> JSON
          </button>
          {canEdit && (
            <button
              data-testid="regenerate-metadata-btn"
              onClick={generate}
              disabled={generating}
              className="flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-widest text-zinc-400 hover:text-[#00E5FF] border border-zinc-800 hover:border-[#00E5FF] px-2 py-1 rounded-sm transition-colors disabled:opacity-50"
            >
              {generating ? <Loader2 size={12} className="animate-spin" /> : <Sparkles size={12} strokeWidth={1.5} />}
              Regenerate
            </button>
          )}
          {canEdit && (
            <button
              data-testid="save-metadata-btn"
              onClick={save}
              disabled={saving}
              className="flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-widest text-black bg-[#00E5FF] px-2 py-1 rounded-sm hover:bg-[#33EFFF] transition-colors disabled:opacity-50"
            >
              {saving ? <Loader2 size={12} className="animate-spin" /> : <Save size={12} strokeWidth={1.8} />}
              Save
            </button>
          )}
        </div>
      </div>

      {/* Titles */}
      <div className="border border-zinc-800 bg-[#121212] rounded-sm">
        <div className="px-5 py-3 border-b border-zinc-800 flex items-center justify-between">
          <span className="font-mono text-[10px] uppercase tracking-widest text-zinc-500">Title options · pick the strongest</span>
          <CopyButton text={draft.selected_title} testId="copy-title" label="Copy selected" />
        </div>
        <div className="divide-y divide-zinc-800">
          {(metadata.title_options || []).map((t, i) => {
            const selected = draft.selected_title === t;
            return (
              <button
                key={`title-${i}-${t.slice(0, 32)}`}
                type="button"
                disabled={!canEdit}
                data-testid={`title-option-${i}`}
                onClick={() => canEdit && setDraft({ ...draft, selected_title: t })}
                className={`w-full text-left px-5 py-3 transition-colors flex items-start gap-3 ${
                  selected ? "bg-[#00E5FF]/5 border-l-2 border-[#00E5FF]" : "hover:bg-[#1A1A1A] border-l-2 border-transparent"
                }`}
              >
                <span className="font-mono text-[10px] text-zinc-500 mt-1">{String(i + 1).padStart(2, "0")}</span>
                <span className="flex-1 text-sm text-zinc-200">{t}</span>
                {selected && <span className="font-mono text-[10px] text-[#00E5FF] uppercase tracking-widest">Selected</span>}
              </button>
            );
          })}
        </div>
      </div>

      {/* Description */}
      <div className="border border-zinc-800 bg-[#121212] rounded-sm">
        <div className="px-5 py-3 border-b border-zinc-800 flex items-center justify-between">
          <span className="font-mono text-[10px] uppercase tracking-widest text-zinc-500">Description</span>
          <CopyButton text={draft.description} testId="copy-description" />
        </div>
        <textarea
          data-testid="description-textarea"
          disabled={!canEdit}
          value={draft.description}
          onChange={(e) => setDraft({ ...draft, description: e.target.value })}
          rows={8}
          className="w-full bg-transparent p-5 text-sm leading-relaxed resize-none focus:outline-none text-zinc-200 disabled:opacity-80"
        />
      </div>

      {/* Tags + Hashtags + Chapters */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div className="border border-zinc-800 bg-[#121212] rounded-sm">
          <div className="px-5 py-3 border-b border-zinc-800 flex items-center justify-between">
            <span className="font-mono text-[10px] uppercase tracking-widest text-zinc-500">Tags (comma-separated)</span>
            <CopyButton text={(metadata.tags || []).join(", ")} testId="copy-tags" />
          </div>
          <textarea
            data-testid="tags-textarea"
            disabled={!canEdit}
            value={draft.tags}
            onChange={(e) => setDraft({ ...draft, tags: e.target.value })}
            rows={4}
            className="w-full bg-transparent p-5 text-sm leading-relaxed resize-none focus:outline-none text-zinc-200 disabled:opacity-80 font-mono text-xs"
          />
        </div>
        <div className="border border-zinc-800 bg-[#121212] rounded-sm">
          <div className="px-5 py-3 border-b border-zinc-800 flex items-center justify-between">
            <span className="font-mono text-[10px] uppercase tracking-widest text-zinc-500">Hashtags</span>
          </div>
          <div className="p-5 flex flex-wrap gap-2">
            {(metadata.hashtags || []).map((h) => (
              <span key={`hashtag-${h}`} className="font-mono text-xs px-2 py-1 border border-zinc-800 text-[#7B61FF] rounded-sm">{h}</span>
            ))}
          </div>
          <div className="border-t border-zinc-800 px-5 py-3">
            <span className="font-mono text-[10px] uppercase tracking-widest text-zinc-500">Chapters</span>
            <ul className="mt-3 space-y-1">
              {(metadata.chapters || []).map((c) => (
                <li key={`chapter-${c.timestamp}-${c.title}`} className="flex items-center gap-3 text-sm">
                  <span className="font-mono text-[11px] text-[#00E5FF] w-14">{c.timestamp}</span>
                  <span className="text-zinc-300">{c.title}</span>
                </li>
              ))}
            </ul>
          </div>
        </div>
      </div>

      {/* Pinned comment */}
      <div className="border border-zinc-800 bg-[#121212] rounded-sm">
        <div className="px-5 py-3 border-b border-zinc-800 flex items-center justify-between">
          <span className="font-mono text-[10px] uppercase tracking-widest text-zinc-500">Pinned comment</span>
          <CopyButton text={draft.pinned_comment} testId="copy-pinned" />
        </div>
        <textarea
          data-testid="pinned-textarea"
          disabled={!canEdit}
          value={draft.pinned_comment}
          onChange={(e) => setDraft({ ...draft, pinned_comment: e.target.value })}
          rows={3}
          className="w-full bg-transparent p-5 text-sm leading-relaxed resize-none focus:outline-none text-zinc-200 disabled:opacity-80"
        />
      </div>
    </div>
  );
}
