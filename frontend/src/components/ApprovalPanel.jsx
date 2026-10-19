import React, { useEffect, useState } from "react";
import { api } from "../lib/api";
import { toast } from "sonner";

export default function ApprovalPanel({ projectId, jobId }) {
  const [manifest, setManifest] = useState(null);
  const [watch, setWatch] = useState({ currentTime: 0, duration: 0, completed: false });
  const [hash, setHash] = useState(null);

  useEffect(() => {
    api.get(`/projects/${projectId}/release/manifest`).then(r => setManifest(r.data)).catch(()=>{});
    api.get(`/projects/${projectId}/qa/report`).then(r => setHash(r.data)).catch(()=>{});
  }, [projectId]);

  const handleWatch = (e) => {
    const ct = e.target.currentTime;
    const dur = e.target.duration || 0;
    const completed = dur && ct/dur > 0.95;
    setWatch({ currentTime: ct, duration: dur, completed });
    api.post(`/projects/${projectId}/watch`, { job_id: jobId, currentTime: ct, duration: dur, completed }).catch(()=>{});
  };

  const approve = async () => {
    if (!watch.completed) {
      toast.error("Must watch entire video before approving");
      return;
    }
    try {
      const { data } = await api.post(`/projects/${projectId}/approve`, { job_id: jobId });
      toast.success("Approved", { description: `video_hash ${data.video_hash.slice(0,12)}...` });
    } catch (e) {
      toast.error(e.response?.data?.detail || e.message);
    }
  };

  if (!manifest) return <div className="text-xs text-slate-500">No manifest — render not completed</div>;

  return (
    <div className="border border-zinc-800 bg-[#121212] p-4 rounded-sm space-y-3">
      <div className="font-mono text-[10px] uppercase tracking-widest text-zinc-500">Exact-version Approval</div>
      <div className="text-xs font-mono break-all">video_hash: {manifest.video_hash}</div>
      <div className="text-xs font-mono break-all">manifest_hash: {hash?.checks ? JSON.stringify(hash).slice(0,60) : "..."}</div>
      <video src={manifest.video_hash ? `/api/projects/${projectId}/download` : ""} controls onTimeUpdate={handleWatch} className="w-full aspect-video bg-black" />
      <label className="flex items-center gap-2 text-xs"><input type="checkbox" checked={watch.completed} readOnly /> I watched entire video ({Math.round(watch.currentTime)}s / {Math.round(watch.duration)}s)</label>
      <button onClick={approve} disabled={!watch.completed} className="w-full bg-blue-600 text-white text-xs font-mono uppercase tracking-widest px-3 py-2 rounded-sm disabled:opacity-50">Approve exact version</button>
      {!watch.completed && <div className="text-[10px] text-amber-400">Approve disabled until watched</div>}
    </div>
  );
}
