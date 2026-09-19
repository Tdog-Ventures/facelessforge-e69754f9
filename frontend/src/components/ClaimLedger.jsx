import React, { useEffect, useState } from "react";
import { api } from "../lib/api";
import { toast } from "sonner";

export default function ClaimLedger({ projectId }) {
  const [items, setItems] = useState([]);
  const [form, setForm] = useState({ exact_wording: "", source_url: "", factual_status: "inference" });
  useEffect(() => { api.get(`/projects/${projectId}/claims`).then(r=>setItems(r.data)).catch(()=>{}); }, [projectId]);
  const add = async () => {
    if (!form.exact_wording) return toast.error("exact wording required");
    await api.post(`/projects/${projectId}/claims`, form);
    setForm({ exact_wording: "", source_url: "", factual_status: "inference" });
    const {data}=await api.get(`/projects/${projectId}/claims`); setItems(data);
  };
  return (
    <div className="border border-zinc-800 bg-[#121212] p-4 rounded-sm space-y-3">
      <div className="font-mono text-[10px] uppercase tracking-widest text-zinc-500">Claim Ledger — {items.length} claims</div>
      {items.map(c=>(
        <div key={c.claim_id} className="text-xs border-b border-zinc-800 py-2">
          <div>"{c.exact_wording}" — {c.factual_status}</div>
          <div className="text-[10px] text-slate-500">{c.source_url} · {c.publisher}</div>
        </div>
      ))}
      <div className="flex gap-2">
        <input value={form.exact_wording} onChange={e=>setForm({...form, exact_wording:e.target.value})} placeholder="Exact wording" className="flex-1 bg-[#0A0A0A] border border-zinc-800 px-2 py-1 text-xs rounded-sm" />
        <input value={form.source_url} onChange={e=>setForm({...form, source_url:e.target.value})} placeholder="Source URL" className="flex-1 bg-[#0A0A0A] border border-zinc-800 px-2 py-1 text-xs rounded-sm" />
        <select value={form.factual_status} onChange={e=>setForm({...form, factual_status:e.target.value})} className="bg-[#0A0A0A] border border-zinc-800 px-2 py-1 text-xs rounded-sm">
          <option>firsthand</option><option>attributed</option><option>measured</option><option>inference</option>
        </select>
        <button onClick={add} className="bg-blue-600 text-white text-xs px-3 py-1 rounded-sm">Add</button>
      </div>
      <div className="text-[10px] text-slate-500">Model never invents first-person testing — use firsthand only with evidence.</div>
    </div>
  );
}
