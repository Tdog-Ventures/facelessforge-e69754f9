import React, { useEffect, useState } from "react";
import { toast } from "sonner";
import { Sparkles, Save, Loader2, PenLine } from "lucide-react";
import { api, formatApiError, UNBOUNDED_TIMEOUT } from "../lib/api";
import { invalidationNotice } from "../lib/scriptUtils";
import CopyButton from "./CopyButton";
import ManualScriptForm from "./ManualScriptForm";

export default function ScriptPanel({ projectId, script, canEdit, onChange }) {
  const [generating, setGenerating] = useState(false);
  const [saving, setSaving] = useState(false);
  const [manualMode, setManualMode] = useState(false);
  const [confirmOverwrite, setConfirmOverwrite] = useState(false);
  const [autoSelectFirst, setAutoSelectFirst] = useState(true);
  const [draft, setDraft] = useState(() => {
    // Persist script immediately: check localStorage first, don't init as '' if saved exists
    try {
      const saved = localStorage.getItem(`script-${projectId}`);
      if (saved) {
        const parsed = JSON.parse(saved);
        if (parsed?.id === script?.id || (!script?.id && parsed?.full_script)) {
          return {
            selected_hook: parsed.selected_hook || script?.selected_hook || "",
            full_script: parsed.full_script || script?.full_script || "",
            cta_block: parsed.cta_block || script?.cta_block || "",
          };
        }
      }
    } catch {}
    return {
      selected_hook: script?.selected_hook || "",
      full_script: script?.full_script || "",
      cta_block: script?.cta_block || "",
    };
  });

  useEffect(() => {
    if (!script?.id) return; // don't clear if fetch returns null — keep persisted draft
    const next = {
      selected_hook: script?.selected_hook || "",
      full_script: script?.full_script || "",
      cta_block: script?.cta_block || "",
    };
    setDraft(next);
    try {
      localStorage.setItem(`script-${projectId}`, JSON.stringify({ id: script.id, ...next }));
    } catch {}
  }, [script?.id]);

  // Auto-select the first hook if the user hasn't picked one within 5 seconds.
  useEffect(() => {
    if (!canEdit || !autoSelectFirst || !script) return;
    const hooks = [script.hook_option_one, script.hook_option_two, script.hook_option_three].filter(Boolean);
    if (!hooks.length || draft.selected_hook) return;
    const timer = setTimeout(() => {
      if (!draft.selected_hook) {
        pickHook(hooks[0]);
      }
    }, 5000);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [script?.id, autoSelectFirst, canEdit]);

  const generate = async () => {
    setGenerating(true);
    toast.loading("Generating with DeepSeek…", { id: "gen-script" });
    try {
      const { data } = await api.post(`/projects/${projectId}/generate-script`, {}, UNBOUNDED_TIMEOUT);
      // Persist immediately to store + backend + localStorage
      const newScript = data?.script || data;
      if (newScript?.full_script) {
        try {
          localStorage.setItem(`script-${projectId}`, JSON.stringify(newScript));
        } catch {}
        setDraft({
          selected_hook: newScript.selected_hook || "",
          full_script: newScript.full_script || "",
          cta_block: newScript.cta_block || "",
        });
      }
      onChange(data);
      toast.success("Script generated", { id: "gen-script" });
      const notice = invalidationNotice(data);
      if (notice) toast.info("Downstream assets need regeneration", { description: notice, duration: 8000 });
    } catch (err) {
      toast.error("Generation failed", { id: "gen-script", description: formatApiError(err.response?.data?.detail) || err.message });
    } finally {
      setGenerating(false);
    }
  };

  const save = async () => {
    setSaving(true);
    try {
      const { data } = await api.patch(`/projects/${projectId}/script`, draft);
      onChange(data);
      const notice = invalidationNotice(data);
      toast.success("Script saved");
      if (notice) toast.info("Downstream assets need regeneration", { description: notice, duration: 8000 });
    } catch (err) {
      toast.error("Save failed", { description: formatApiError(err.response?.data?.detail) || err.message });
    } finally {
      setSaving(false);
    }
  };

  const startGenerate = () => {
    // Explicit user action only: never silently overwrite a manual script.
    if (script?.source === "manual" && !confirmOverwrite) {
      setConfirmOverwrite(true);
      return;
    }
    setConfirmOverwrite(false);
    generate();
  };

  const pickHook = (hook) => {
    setDraft({ ...draft, selected_hook: hook });
  };

  if (!script) {
    // Show skeleton while generating, don't unmount component
    if (generating) {
      return (
        <div className="space-y-6 animate-pulse">
          <div className="h-6 bg-[var(--ff-surface-2)] rounded w-1/3" />
          <div className="h-32 bg-[var(--ff-surface-2)] rounded" />
          <div className="h-6 bg-[var(--ff-surface-2)] rounded w-1/4" />
        </div>
      );
    }
    // Try localStorage fallback before showing empty
    let hasLocal = false;
    try {
      const saved = localStorage.getItem(`script-${projectId}`);
      hasLocal = !!(saved && JSON.parse(saved)?.full_script);
    } catch {}
    if (hasLocal) {
      // Will be picked up by useEffect on next render — show skeleton briefly
      return (
        <div className="border border-[var(--ff-border)] p-10 text-center rounded-sm">
          <Loader2 size={18} className="animate-spin text-[#00E5FF] mx-auto mb-3" />
          <p className="text-sm text-[var(--ff-text-muted)]">Restoring script…</p>
        </div>
      );
    }
    return (
      <div className="border border-[var(--ff-border)] border-dashed p-10 text-center rounded-sm space-y-4">
        <p className="text-sm text-[var(--ff-text-muted)]">No script yet. Generate one to get started.</p>
        {canEdit && !manualMode && (
          <div className="flex items-center justify-center gap-3 flex-wrap">
            <button
              data-testid="generate-script-btn"
              onClick={generate}
              disabled={generating}
              className="inline-flex items-center gap-2 bg-[#00E5FF] text-black font-semibold text-sm px-4 py-2 rounded-sm hover:bg-[#33EFFF] disabled:opacity-60 transition-colors"
            >
              <Loader2 size={14} className={generating ? "animate-spin" : "hidden"} />
              <Sparkles size={14} strokeWidth={2} className={generating ? "hidden" : "block"} />
              {generating ? "Generating…" : "Generate Script with AI"}
            </button>
            <button
              data-testid="manual-script-btn"
              onClick={() => setManualMode(true)}
              disabled={generating}
              className="inline-flex items-center gap-2 border border-[var(--ff-input-border)] text-[var(--ff-text)] font-semibold text-sm px-4 py-2 rounded-sm hover:border-[#00E5FF] hover:text-[#00E5FF] disabled:opacity-60 transition-colors"
            >
              <PenLine size={14} strokeWidth={2} />
              Write or Paste Script
            </button>
          </div>
        )}
        {canEdit && manualMode && (
          <div className="text-left">
            <ManualScriptForm
              projectId={projectId}
              onSaved={(data) => {
                setManualMode(false);
                onChange(data);
              }}
              onCancel={() => setManualMode(false)}
            />
          </div>
        )}
      </div>
    );
  }

  const hooks = [script.hook_option_one, script.hook_option_two, script.hook_option_three].filter(Boolean);

  return (
    <div className="space-y-6">
      {confirmOverwrite && (
        <div
          data-testid="regenerate-confirm"
          className="border border-amber-500/50 bg-amber-500/5 rounded-sm px-4 py-3 flex items-center gap-3 flex-wrap"
        >
          <p className="flex-1 text-sm text-amber-200 min-w-[200px]">
            This script was written manually. Replace it with AI output?
          </p>
          <button
            data-testid="confirm-overwrite-btn"
            onClick={generate}
            disabled={generating}
            className="font-mono text-[10px] uppercase tracking-widest bg-amber-500 text-black px-3 py-1.5 rounded-sm hover:bg-amber-400 disabled:opacity-50"
          >
            Replace with AI
          </button>
          <button
            data-testid="cancel-overwrite-btn"
            onClick={() => setConfirmOverwrite(false)}
            className="font-mono text-[10px] uppercase tracking-widest text-[var(--ff-text-muted)] hover:text-[var(--ff-text)] border border-[var(--ff-input-border)] px-3 py-1.5 rounded-sm"
          >
            Keep mine
          </button>
        </div>
      )}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-4 font-mono text-[11px] text-[var(--ff-text-muted)]">
          <span>{script?.word_count ?? 0} words</span>
          <span>·</span>
          <span>~{Math.floor(script.estimated_duration / 60)}:{String(script.estimated_duration % 60).padStart(2, "0")} narration</span>
          {script?.source && (
            <span
              data-testid="script-source-badge"
              className={`uppercase tracking-widest border px-1.5 py-0.5 rounded-sm ${
                script.source === "manual"
                  ? "text-[#00E5FF] border-[#00E5FF]/40"
                  : "text-[var(--ff-text-muted)] border-[var(--ff-input-border)]"
              }`}
            >
              {script.source === "manual" ? "Manual" : "AI"}
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          {canEdit && (
            <button
              data-testid="regenerate-script-btn"
              onClick={startGenerate}
              disabled={generating}
              className="flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-widest text-[var(--ff-text-muted)] hover:text-[#00E5FF] border border-[var(--ff-border)] hover:border-[#00E5FF] px-2 py-1 rounded-sm transition-colors disabled:opacity-50"
            >
              {generating ? <Loader2 size={12} className="animate-spin" /> : <Sparkles size={12} strokeWidth={1.5} />}
              Regenerate
            </button>
          )}
          {canEdit && (
            <button
              data-testid="save-script-btn"
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

      {/* Hook options */}
      <div className="border border-[var(--ff-border)] bg-[var(--ff-surface)] rounded-sm">
        <div className="px-5 py-3 border-b border-[var(--ff-border)] flex items-center justify-between">
          <span className="font-mono text-[10px] uppercase tracking-widest text-[var(--ff-text-muted)]">Hook options · pick one</span>
          {canEdit && (
            <label className="flex items-center gap-2 text-xs text-[var(--ff-text-muted)] cursor-pointer">
              <input
                type="checkbox"
                checked={autoSelectFirst}
                onChange={(e) => setAutoSelectFirst(e.target.checked)}
                className="h-3 w-3 accent-[#00E5FF] bg-[var(--ff-input-bg)] border-[var(--ff-input-border)] rounded"
              />
              Auto-select first hook
            </label>
          )}
        </div>
        <div className="divide-y divide-zinc-800">
          {hooks.map((h, i) => {
            const selected = draft.selected_hook === h;
            return (
              <button
                key={`hook-${i}-${h.slice(0, 24)}`}
                type="button"
                disabled={!canEdit}
                data-testid={`hook-option-${i}`}
                onClick={() => canEdit && pickHook(h)}
                className={`w-full text-left p-5 transition-colors ${
                  selected ? "bg-[#00E5FF]/5 border-l-2 border-[#00E5FF]" : "hover:bg-[var(--ff-surface-2)] border-l-2 border-transparent"
                }`}
              >
                <div className="flex items-start gap-3">
                  <span className="font-mono text-[10px] uppercase tracking-widest text-[var(--ff-text-muted)] mt-1">
                    Hook {i + 1}
                  </span>
                  <p className="flex-1 text-sm text-[var(--ff-text)] leading-relaxed">{h}</p>
                  {selected && <span className="font-mono text-[10px] text-[#00E5FF] uppercase tracking-widest">Selected</span>}
                </div>
              </button>
            );
          })}
        </div>
      </div>

      {/* Selected hook readonly / full script editor */}
      <div className="border border-[var(--ff-border)] bg-[var(--ff-surface)] rounded-sm">
        <div className="px-5 py-3 border-b border-[var(--ff-border)] flex items-center justify-between">
          <span className="font-mono text-[10px] uppercase tracking-widest text-[var(--ff-text-muted)]">Full script</span>
          <CopyButton text={draft.full_script} testId="copy-script" />
        </div>
        <textarea
          data-testid="script-textarea"
          disabled={!canEdit}
          value={draft.full_script}
          onChange={(e) => setDraft({ ...draft, full_script: e.target.value })}
          rows={16}
          className="w-full bg-transparent p-5 text-sm leading-relaxed resize-none focus:outline-none text-[var(--ff-text)] disabled:opacity-80"
        />
      </div>

      {/* Retention beats */}
      {script.retention_beats?.length > 0 && (
        <div className="border border-[var(--ff-border)] bg-[var(--ff-surface)] p-5 rounded-sm">
          <div className="font-mono text-[10px] uppercase tracking-widest text-[var(--ff-text-muted)] mb-3">Retention beats</div>
          <ul className="space-y-2">
            {script.retention_beats.map((b, i) => (
              <li key={`beat-${i}-${String(b).slice(0, 32)}`} className="flex items-start gap-3 text-sm text-[var(--ff-text)]">
                <span className="font-mono text-[10px] text-[#7B61FF] mt-1">{String(i + 1).padStart(2, "0")}</span>
                <span>{b}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* CTA */}
      <div className="border border-[var(--ff-border)] bg-[var(--ff-surface)] rounded-sm">
        <div className="px-5 py-3 border-b border-[var(--ff-border)] flex items-center justify-between">
          <span className="font-mono text-[10px] uppercase tracking-widest text-[var(--ff-text-muted)]">CTA block</span>
          <CopyButton text={draft.cta_block} testId="copy-cta" />
        </div>
        <textarea
          data-testid="cta-textarea"
          disabled={!canEdit}
          value={draft.cta_block}
          onChange={(e) => setDraft({ ...draft, cta_block: e.target.value })}
          rows={4}
          className="w-full bg-transparent p-5 text-sm leading-relaxed resize-none focus:outline-none text-[var(--ff-text)] disabled:opacity-80"
        />
      </div>
    </div>
  );
}
