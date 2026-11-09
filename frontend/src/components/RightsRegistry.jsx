import React, { useEffect, useState } from "react";
import { api } from "../lib/api";

export default function RightsRegistry({ projectId }) {
  const [items, setItems] = useState([]);
  useEffect(() => { api.get(`/projects/${projectId}/rights`).then(r=>setItems(r.data)).catch(()=>{}); }, [projectId]);
  return (
    <div className="border border-[var(--ff-border)] bg-[var(--ff-surface)] p-4 rounded-sm">
      <div className="font-mono text-[10px] uppercase tracking-widest text-[var(--ff-text-muted)] mb-3">Rights Registry — {items.length} assets</div>
      {items.map((it,i)=>(
        <div key={i} className="text-xs font-mono border-b border-[var(--ff-border)] py-2">
          <div>asset {it.asset_id} · {it.source} · {it.licence || it.license || "unspecified"}</div>
          <div className="text-[10px] text-[var(--ff-text-muted)]">{it.licence_url} · {it.sha256?.slice(0,16)}...</div>
        </div>
      ))}
      {items.length===0 && <div className="text-xs text-[var(--ff-text-muted)]">No rights yet — assets archived at acquisition in external_api.py</div>}
    </div>
  );
}
