import React, { useEffect, useState } from "react";
import { toast } from "sonner";
import { Sparkles, Save, Loader2 } from "lucide-react";
import { api, formatApiError } from "../lib/api";
import CopyButton from "./CopyButton";

export default function ScriptPanel({ projectId, script, canEdit, onChange }) {
  const [generating, setGenerating] = useState(false);
  const [saving, setSaving] = useState(false);
  const [draft, setDraft] = useState({
    selected_hook: script?.selected_hook || "",
    full_script: script?.full_script || "",
    cta_block: script?.cta_block || "",
  });

  useEffect(() => {
    setDraft({
      selected_hook: script?.selected_hook || "",
      full_script: script?.full_script || "",
      cta_block: script?.cta_block || "",
    });
  }, [script?.id]);

  const generate = async () => {
    setGenerating(true);
    toast.loading("Generating script with GPT-5.2…", { id: "gen-script" });
    try {
      const { data } = await api.post(`/projects/${projectId}/generate-script`);
      onChange(data);
      toast.success("Script generated", { id: "gen-script" });
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
      toast.success("Script saved");
    } catch (err) {
      toast.error("Save failed", { description: formatApiError(err.response?.data?.detail) || err.message });
    } finally {
      setSaving(false);
    }
  };

  const pickHook = (hook) => {
    setDraft({ ...draft, selected_hook: hook });
  };

  if (!script) {
    return (
      <div className="border border-zinc-800 border-dashed p-10 text-center rounded-sm">
        <p className="text-sm text-zinc-400 mb-4">No script yet. Generate one to get started.</p>
        {canEdit && (
          <button
            data-testid="generate-script-btn"
            onClick={generate}
            disabled={generating}
            className="inline-flex items-center gap-2 bg-[#00E5FF] text-black font-semibold text-sm px-4 py-2 rounded-sm hover:bg-[#33EFFF] disabled:opacity-60 transition-colors"
          >
            {generating ? <Loader2 size={14} className="animate-spin" /> : <Sparkles size={14} strokeWidth={2} />}
            {generating ? "Generating…" : "Generate Script"}
          </button>
        )}
      </div>
    );
  }

  const hooks = [script.hook_option_one, script.hook_option_two, script.hook_option_three].filter(Boolean);

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-4 font-mono text-[11px] text-zinc-500">
          <span>{script.word_count} words</span>
          <span>·</span>
          <span>~{Math.floor(script.estimated_duration / 60)}:{String(script.estimated_duration % 60).padStart(2, "0")} narration</span>
        </div>
        <div className="flex items-center gap-2">
          {canEdit && (
            <button
              data-testid="regenerate-script-btn"
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
      <div className="border border-zinc-800 bg-[#121212] rounded-sm">
        <div className="px-5 py-3 border-b border-zinc-800 flex items-center justify-between">
          <span className="font-mono text-[10px] uppercase tracking-widest text-zinc-500">Hook options · pick one</span>
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
                  selected ? "bg-[#00E5FF]/5 border-l-2 border-[#00E5FF]" : "hover:bg-[#1A1A1A] border-l-2 border-transparent"
                }`}
              >
                <div className="flex items-start gap-3">
                  <span className="font-mono text-[10px] uppercase tracking-widest text-zinc-500 mt-1">
                    Hook {i + 1}
                  </span>
                  <p className="flex-1 text-sm text-white leading-relaxed">{h}</p>
                  {selected && <span className="font-mono text-[10px] text-[#00E5FF] uppercase tracking-widest">Selected</span>}
                </div>
              </button>
            );
          })}
        </div>
      </div>

      {/* Selected hook readonly / full script editor */}
      <div className="border border-zinc-800 bg-[#121212] rounded-sm">
        <div className="px-5 py-3 border-b border-zinc-800 flex items-center justify-between">
          <span className="font-mono text-[10px] uppercase tracking-widest text-zinc-500">Full script</span>
          <CopyButton text={draft.full_script} testId="copy-script" />
        </div>
        <textarea
          data-testid="script-textarea"
          disabled={!canEdit}
          value={draft.full_script}
          onChange={(e) => setDraft({ ...draft, full_script: e.target.value })}
          rows={16}
          className="w-full bg-transparent p-5 text-sm leading-relaxed resize-none focus:outline-none text-zinc-200 disabled:opacity-80"
        />
      </div>

      {/* Retention beats */}
      {script.retention_beats?.length > 0 && (
        <div className="border border-zinc-800 bg-[#121212] p-5 rounded-sm">
          <div className="font-mono text-[10px] uppercase tracking-widest text-zinc-500 mb-3">Retention beats</div>
          <ul className="space-y-2">
            {script.retention_beats.map((b, i) => (
              <li key={`beat-${i}-${String(b).slice(0, 32)}`} className="flex items-start gap-3 text-sm text-zinc-300">
                <span className="font-mono text-[10px] text-[#7B61FF] mt-1">{String(i + 1).padStart(2, "0")}</span>
                <span>{b}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* CTA */}
      <div className="border border-zinc-800 bg-[#121212] rounded-sm">
        <div className="px-5 py-3 border-b border-zinc-800 flex items-center justify-between">
          <span className="font-mono text-[10px] uppercase tracking-widest text-zinc-500">CTA block</span>
          <CopyButton text={draft.cta_block} testId="copy-cta" />
        </div>
        <textarea
          data-testid="cta-textarea"
          disabled={!canEdit}
          value={draft.cta_block}
          onChange={(e) => setDraft({ ...draft, cta_block: e.target.value })}
          rows={4}
          className="w-full bg-transparent p-5 text-sm leading-relaxed resize-none focus:outline-none text-zinc-200 disabled:opacity-80"
        />
      </div>
    </div>
  );
}
