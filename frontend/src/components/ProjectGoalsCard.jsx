import React, { useEffect, useState } from "react";
import { toast } from "sonner";
import { Loader2, Save } from "lucide-react";
import { api, formatApiError } from "../lib/api";

const GOALS = [
  "Grow channel to 50k subs",
  "Sell my product",
  "Build authority",
  "Drive affiliate sales",
  "Educate audience",
  "Generate leads",
];
const CTAS = ["Visit website", "Subscribe", "Buy product", "Join newsletter", "Book a call"];

function filled(value) {
  return Boolean(String(value || "").trim());
}

export function goalsComplete(project) {
  return filled(project?.audience) && filled(project?.monetisation_intent) && filled(project?.cta_goal);
}

export default function ProjectGoalsCard({ projectId, project, onChange, canEdit }) {
  const complete = goalsComplete(project);
  const [open, setOpen] = useState(!complete);
  const [audience, setAudience] = useState(project?.audience || "");
  const [goal, setGoal] = useState(project?.monetisation_intent || "");
  const [cta, setCta] = useState(project?.cta_goal || "");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    const audienceValue = project?.audience || "";
    const goalValue = project?.monetisation_intent || "";
    const ctaValue = project?.cta_goal || "";
    setAudience(audienceValue);
    setGoal(goalValue);
    setCta(ctaValue);
    setOpen(!(filled(audienceValue) && filled(goalValue) && filled(ctaValue)));
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
    <div className="bg-[var(--ff-surface)] border border-[rgba(59,130,246,0.15)] rounded-lg p-5 space-y-4" data-testid="project-goals-card">
      <div className="flex items-center justify-between gap-3">
        <h3 className="font-mono text-[11px] uppercase tracking-widest text-[var(--ff-text-muted)]">Project Goals</h3>
        {complete && !open && (
          <button
            type="button"
            data-testid="edit-project-goals"
            onClick={() => setOpen(true)}
            className="font-mono text-[10px] uppercase tracking-widest text-[var(--ff-text)] underline"
          >
            Edit
          </button>
        )}
      </div>
      {complete && !open ? (
        <p data-testid="project-goals-summary" className="text-sm text-[var(--ff-text)]">
          {project.audience} · {project.monetisation_intent} · {project.cta_goal}
        </p>
      ) : (
        <div className="space-y-4">
          <label className="block space-y-1.5 text-sm font-medium text-[var(--ff-text)]">
            Who's watching?
            <input
              data-testid="field-audience-human"
              value={audience}
              onChange={(e) => setAudience(e.target.value)}
              placeholder="e.g. Gen Z tech hobbyists, busy pros"
              disabled={!canEdit}
              className="w-full bg-[var(--ff-bg)] border border-[var(--ff-input-border)] rounded-md px-3 py-2 text-sm text-[var(--ff-text)] placeholder:text-[var(--ff-input-placeholder)] focus:border-blue-500 disabled:opacity-60"
            />
          </label>
          <label className="block space-y-1.5 text-sm font-medium text-[var(--ff-text)]">
            What's the goal?
            <select
              data-testid="field-goal-human"
              value={goal}
              onChange={(e) => setGoal(e.target.value)}
              disabled={!canEdit}
              className="w-full bg-[var(--ff-bg)] border border-[var(--ff-input-border)] rounded-md px-3 py-2 text-sm text-[var(--ff-text)] focus:border-blue-500 disabled:opacity-60"
            >
              <option value="">Choose a goal</option>
              {GOALS.map((item) => <option key={item} value={item}>{item}</option>)}
            </select>
          </label>
          <label className="block space-y-1.5 text-sm font-medium text-[var(--ff-text)]">
            What should they do?
            <select
              data-testid="field-cta-human"
              value={cta}
              onChange={(e) => setCta(e.target.value)}
              disabled={!canEdit}
              className="w-full bg-[var(--ff-bg)] border border-[var(--ff-input-border)] rounded-md px-3 py-2 text-sm text-[var(--ff-text)] focus:border-blue-500 disabled:opacity-60"
            >
              <option value="">Choose an action</option>
              {CTAS.map((item) => <option key={item} value={item}>{item}</option>)}
            </select>
          </label>
          {canEdit && (
            <button
              type="button"
              data-testid="save-human-params"
              onClick={save}
              disabled={saving}
              className="w-full flex items-center justify-center gap-2 bg-blue-600 text-white text-sm font-semibold py-2 rounded-md hover:bg-blue-500 disabled:opacity-50"
            >
              {saving ? <Loader2 size={14} className="animate-spin" /> : <Save size={14} />} Save
            </button>
          )}
        </div>
      )}
    </div>
  );
}
