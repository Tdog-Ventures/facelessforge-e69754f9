import React, { useEffect, useState } from "react";
import { api } from "../lib/api";

export default function RightsRegistry({ projectId }) {
  const [items, setItems] = useState([]);
  useEffect(() => { api.get(`/projects/${projectId}/rights`).then(r=>setItems(r.data)).catch(()=>{}); }, [projectId]);
  return (
    <div className="border border-zinc-800 bg-[#121212] p-4 rounded-sm">
      <div className="font-mono text-[10px] uppercase tracking-widest text-zinc-500 mb-3">Rights Registry — {items.length} assets</div>
      {items.map((it,i)=>(
        <div key={i} className="text-xs font-mono border-b border-zinc-800 py-2">
          <div>asset {it.asset_id} · {it.source} · {it.licence || it.license || "unspecified"}</div>
          <div className="text-[10px] text-slate-500">{it.licence_url} · {it.sha256?.slice(0,16)}...</div>
        </div>
      ))}
      {items.length===0 && <div className="text-xs text-slate-500">No rights yet — assets archived at acquisition in external_api.py</div>}
    </div>
  );
}
