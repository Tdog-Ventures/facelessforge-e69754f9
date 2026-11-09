import React, { useEffect, useState } from "react";
import { api } from "../lib/api";

export default function ProductionReceipt({ projectId }) {
  const [data, setData] = useState(null);
  useEffect(() => {
    // Fetch last render job for receipt
    api.get(`/projects/${projectId}/render/jobs`).then(r=>{
      const job = r.data?.[0];
      if (job) {
        const turnaround = job.duration || 0;
        const human_minutes = Math.round(turnaround/60*0.2);
        const variable = { deepseek: 0.01, elevenlabs: (job.duration||0)/1000*0.10, ffmpeg: 0, storage: 0.01 };
        setData({ turnaround_seconds: turnaround, human_minutes, rework_reserve: 2, fully_loaded_cost: (variable.deepseek+variable.elevenlabs+0.05).toFixed(2), variable_cost: variable });
      }
    }).catch(()=>{});
  }, [projectId]);
  if (!data) return <div className="text-xs text-[var(--ff-text-muted)]">No receipt yet — render a video</div>;
  return (
    <div className="border border-[var(--ff-border)] bg-[var(--ff-surface)] p-4 rounded-sm font-mono text-xs">
      <div className="uppercase tracking-widest text-[var(--ff-text-muted)] mb-2">Production Receipt</div>
      <div>Turnaround: {data.turnaround_seconds}s</div>
      <div>Human minutes: {data.human_minutes}</div>
      <div>Rework reserve: {data.rework_reserve} min</div>
      <div>Fully loaded: ${data.fully_loaded_cost}</div>
      <div className="text-[10px] text-[var(--ff-text-muted)] mt-2">DeepSeek ${data.variable_cost.deepseek} + ElevenLabs ${data.variable_cost.elevenlabs.toFixed(2)} + FFmpeg $0 + storage $0.01</div>
    </div>
  );
}
