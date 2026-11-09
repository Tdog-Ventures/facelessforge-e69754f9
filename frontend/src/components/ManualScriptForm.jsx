import React, { useMemo, useState } from "react";
import { toast } from "sonner";
import { Save, Loader2, X } from "lucide-react";
import { api, formatApiError } from "../lib/api";
import {
  countWords,
  formatNarration,
  estimateDurationSeconds,
  validateManualScript,
  invalidationNotice,
  saveScript,
} from "../lib/scriptUtils";

export default function ManualScriptForm({ projectId, initialText = "", onSaved, onCancel }) {
  const [text, setText] = useState(initialText);
  const [saving, setSaving] = useState(false);
  const [serverError, setServerError] = useState(null);

  const stats = useMemo(() => {
    const words = countWords(text);
    return { words, seconds: estimateDurationSeconds(words) };
  }, [text]);
  const validation = useMemo(() => validateManualScript(text), [text]);

  const save = async () => {
    if (!validation.ok || saving) return;
    setSaving(true);
    setServerError(null);
    try {
      const data = await saveScript(api, projectId, {
        full_script: validation.cleaned,
        source: "manual",
      });
      const notice = invalidationNotice(data);
      onSaved(data);
      toast.success("Script saved");
      if (notice) toast.info("Downstream assets need regeneration", { description: notice, duration: 8000 });
    } catch (err) {
      const msg = err.message || formatApiError(err) || "Save failed";
      setServerError(msg);
      toast.error("Save failed", { description: msg });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div data-testid="manual-script-form" className="border border-[var(--ff-border)] bg-[var(--ff-surface)] rounded-sm">
      <div className="px-5 py-3 border-b border-[var(--ff-border)] flex items-center justify-between">
        <span className="font-mono text-[10px] uppercase tracking-widest text-[var(--ff-text-muted)]">
          Write or paste script · no AI used
        </span>
        <span data-testid="manual-script-stats" className="font-mono text-[11px] text-[var(--ff-text-muted)]">
          {stats.words} words · ~{formatNarration(stats.seconds)} narration
        </span>
      </div>
      <textarea
        data-testid="manual-script-textarea"
        value={text}
        onChange={(e) => setText(e.target.value)}
        rows={14}
        placeholder="Paste or write the full voiceover script here…"
        className="w-full bg-transparent p-5 text-sm leading-relaxed resize-y focus:outline-none text-[var(--ff-text)] placeholder:text-[var(--ff-input-placeholder)] min-h-[240px]"
      />
      {!validation.ok && text.trim() && (
        <p data-testid="manual-script-error" className="px-5 pb-2 font-mono text-[11px] text-amber-400">
          {validation.error}
        </p>
      )}
      {serverError && (
        <p data-testid="manual-script-server-error" className="px-5 pb-2 font-mono text-[11px] text-[#FF3366]">
          {serverError}
        </p>
      )}
      <div className="px-5 py-3 border-t border-[var(--ff-border)] flex items-center gap-2">
        <button
          data-testid="save-manual-script-btn"
          onClick={save}
          disabled={!validation.ok || saving}
          className="flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-widest text-black bg-[#00E5FF] px-3 py-2 rounded-sm hover:bg-[#33EFFF] transition-colors disabled:opacity-50"
        >
          {saving ? <Loader2 size={12} className="animate-spin" /> : <Save size={12} strokeWidth={1.8} />}
          Save Script
        </button>
        <button
          data-testid="cancel-manual-script-btn"
          onClick={onCancel}
          disabled={saving}
          className="flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-widest text-[var(--ff-text-muted)] hover:text-[var(--ff-text)] border border-[var(--ff-border)] px-3 py-2 rounded-sm transition-colors disabled:opacity-50"
        >
          <X size={12} /> Cancel
        </button>
      </div>
    </div>
  );
}
