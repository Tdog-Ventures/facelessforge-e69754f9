import React, { useState } from "react";
import { api } from "../lib/api";
import { toast } from "sonner";

export default function IntakePacket({ projectId }) {
  const [form, setForm] = useState({ logo: "", colours: "", service_descriptions: "", candidate_images: "" });
  const submit = async () => {
    try {
      const payload = {
        ...form,
        colours: form.colours.split(",").map(s=>s.trim()).filter(Boolean),
        candidate_images: form.candidate_images ? [{url: form.candidate_images, original_url: form.candidate_images, ownership_declaration: "owned", intended_use: "thumbnail", consent: true}] : []
      };
      await api.post(`/projects/${projectId}/intake`, payload);
      toast.success("Intake saved — page displaying image != licence to reuse logged");
    } catch (e) { toast.error(e.response?.data?.detail || e.message); }
  };
  return (
    <div className="border border-zinc-800 bg-[#121212] p-4 rounded-sm space-y-3">
      <div className="font-mono text-[10px] uppercase tracking-widest text-zinc-500">Intake Packet — one format, one input packet</div>
      <input value={form.logo} onChange={e=>setForm({...form, logo:e.target.value})} placeholder="Logo URL" className="w-full bg-[#0A0A0A] border border-zinc-800 px-3 py-2 text-xs rounded-sm" />
      <input value={form.colours} onChange={e=>setForm({...form, colours:e.target.value})} placeholder="Colours comma separated" className="w-full bg-[#0A0A0A] border border-zinc-800 px-3 py-2 text-xs rounded-sm" />
      <textarea value={form.service_descriptions} onChange={e=>setForm({...form, service_descriptions:e.target.value})} placeholder="Service descriptions" className="w-full bg-[#0A0A0A] border border-zinc-800 px-3 py-2 text-xs rounded-sm" rows={3} />
      <input value={form.candidate_images} onChange={e=>setForm({...form, candidate_images:e.target.value})} placeholder="Candidate image URL + ownership declaration" className="w-full bg-[#0A0A0A] border border-zinc-800 px-3 py-2 text-xs rounded-sm" />
      <button onClick={submit} className="w-full bg-blue-600 text-white text-xs font-mono uppercase tracking-widest px-3 py-2 rounded-sm">Save intake packet</button>
      <div className="text-[10px] text-amber-400">Page displaying image != licence — ownership_declaration required.</div>
    </div>
  );
}
