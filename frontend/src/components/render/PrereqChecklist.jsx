import React from "react";
import { CheckCircle2, AlertCircle, RefreshCw } from "lucide-react";

export default function PrereqChecklist({ preflight, onRefresh }) {
  return (
    <div data-testid="render-checklist" className="border border-zinc-800 bg-[#121212] p-6 rounded-sm">
      <div className="flex items-center justify-between mb-4">
        <div className="font-mono text-[11px] uppercase tracking-widest text-zinc-500">
          Prerequisites
        </div>
        <button
          data-testid="render-refresh"
          onClick={onRefresh}
          className="flex items-center gap-1 font-mono text-[10px] uppercase tracking-widest text-zinc-400 hover:text-[#00E5FF]"
        >
          <RefreshCw size={11} /> Refresh
        </button>
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
        {preflight.checklist.map((c) => (
          <div
            key={c.key}
            data-testid={`pre-${c.key}`}
            className={`flex items-start gap-3 p-3 border rounded-sm ${
              c.ok ? "border-[#00FF66]/25 bg-[#00FF66]/5"
                   : "border-[#FF3366]/30 bg-[#FF3366]/5"
            }`}
          >
            {c.ok ? (
              <CheckCircle2 size={16} className="text-[#00FF66] mt-0.5" />
            ) : (
              <AlertCircle size={16} className="text-[#FF3366] mt-0.5" />
            )}
            <div className="flex-1 min-w-0">
              <div className="text-sm text-white">{c.label}</div>
              <div className="font-mono text-[10px] text-zinc-500 mt-1">{c.hint}</div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
