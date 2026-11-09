import React, { useEffect, useState } from "react";
import { api } from "../lib/api";

export default function ComplianceChecklist({ projectId }) {
  const [data, setData] = useState(null);
  useEffect(() => { api.get(`/projects/${projectId}/compliance/checklist`).then(r=>setData(r.data)).catch(()=>{}); }, [projectId]);
  if (!data) return <div className="text-xs text-[var(--ff-text-muted)]">Loading checklist…</div>;
  const Section = ({ title, items }) => (
    <div className="border border-[var(--ff-border)] bg-[var(--ff-surface)] p-3 rounded-sm">
      <div className="font-mono text-[10px] uppercase tracking-widest text-[var(--ff-text-muted)] mb-2">{title}</div>
      <ul className="space-y-1">
        {items.map((it,i)=><li key={i} className="text-xs flex gap-2"><input type="checkbox" /> <span>{it}</span></li>)}
      </ul>
    </div>
  );
  return (
    <div className="space-y-3">
      <p className="font-mono text-[10px] uppercase tracking-widest text-[var(--ff-text-muted)]">
        Static publishing guide. These boxes are not saved and do not check this project.
      </p>
      <Section title="Mandatory" items={data.mandatory} />
      <Section title="Monetization-risk" items={data.monetization_risk} />
      <Section title="Rights" items={data.rights} />
      <Section title="Technical" items={data.technical} />
      <Section title="Human" items={data.human} />
      <Section title="Publishing" items={data.publishing} />
    </div>
  );
}
