import React from "react";
import { Loader2, Star, X as XIcon, Trash2, Download } from "lucide-react";

function fmtSec(s) {
  if (!s && s !== 0) return "—";
  const m = Math.floor(s / 60);
  const r = Math.round(s % 60);
  return m > 0 ? `${m}m ${String(r).padStart(2, "0")}s` : `${r}s`;
}

export default function VoiceoverCard({
  asset, label, isSelectedForProject = false, working, canEdit,
  onSelect, onReject, onDelete,
}) {
  const isSelected = asset.status === "selected" || isSelectedForProject;
  const isRejected = asset.status === "rejected";
  const isWorking = working === asset.id;

  return (
    <div
      data-testid={`vo-card-${asset.id}`}
      className={`border rounded-sm bg-[#0F0F10] p-4 space-y-3 ${
        isSelected ? "border-[#00FF66] shadow-[0_0_0_1px_#00FF66]" :
        isRejected ? "border-[#FF3366]/40 opacity-70" :
        "border-zinc-800"
      }`}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="space-y-1 flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="font-mono text-[10px] uppercase tracking-widest text-zinc-500">
              {label}
            </span>
            <span
              className="font-mono text-[9px] uppercase tracking-widest px-1.5 py-0.5 rounded-sm border"
              style={
                isSelected
                  ? { color: "#00FF66", background: "rgba(0,255,102,0.12)", borderColor: "#00FF66" }
                  : isRejected
                  ? { color: "#FF3366", background: "rgba(255,51,102,0.1)", borderColor: "#FF3366" }
                  : asset.mock
                  ? { color: "#FFB020", background: "rgba(255,176,32,0.1)", borderColor: "#FFB020" }
                  : { color: "#00E5FF", background: "rgba(0,229,255,0.1)", borderColor: "#00E5FF" }
              }
            >
              {isSelected ? "Selected" : isRejected ? "Rejected" : asset.mock ? "Mock" : "Generated"}
            </span>
            <span className="font-mono text-[10px] text-zinc-500">
              {asset.voice_style} · {fmtSec(asset.duration)}
            </span>
          </div>
          {asset.text_excerpt && (
            <p className="text-xs text-zinc-400 line-clamp-2 leading-relaxed">
              {asset.text_excerpt}
            </p>
          )}
        </div>
      </div>

      <audio
        data-testid={`vo-audio-${asset.id}`}
        controls
        src={asset.preview_url}
        className="w-full h-10"
        preload="metadata"
      />

      {canEdit && (
        <div className="flex flex-wrap items-center gap-2 pt-1">
          {!isSelected && !isRejected && (
            <button
              data-testid={`vo-select-${asset.id}`}
              onClick={() => onSelect(asset)}
              disabled={isWorking}
              className="flex items-center gap-1 font-mono text-[10px] uppercase tracking-widest bg-[#00FF66] text-black hover:bg-[#33FF80] px-2 py-1 rounded-sm disabled:opacity-50"
            >
              {isWorking ? <Loader2 size={11} className="animate-spin" /> : <Star size={11} strokeWidth={2} />}
              Select
            </button>
          )}
          {!isRejected && (
            <button
              data-testid={`vo-reject-${asset.id}`}
              onClick={() => onReject(asset)}
              disabled={isWorking}
              className="flex items-center gap-1 font-mono text-[10px] uppercase tracking-widest border border-[#FF3366]/40 text-[#FF3366] hover:bg-[#FF3366]/10 px-2 py-1 rounded-sm disabled:opacity-50"
            >
              <XIcon size={11} strokeWidth={2} />
              Reject
            </button>
          )}
          <a
            data-testid={`vo-download-${asset.id}`}
            href={asset.preview_url}
            download
            className="flex items-center gap-1 font-mono text-[10px] uppercase tracking-widest border border-zinc-700 text-zinc-300 hover:border-[#00E5FF] hover:text-[#00E5FF] px-2 py-1 rounded-sm transition-colors"
          >
            <Download size={11} strokeWidth={1.5} /> Download
          </a>
          <button
            data-testid={`vo-delete-${asset.id}`}
            onClick={() => onDelete(asset)}
            disabled={isWorking}
            className="flex items-center gap-1 font-mono text-[10px] uppercase tracking-widest text-zinc-500 hover:text-[#FF3366] hover:border-[#FF3366] border border-zinc-800 px-2 py-1 rounded-sm transition-colors disabled:opacity-50 ml-auto"
          >
            {isWorking ? <Loader2 size={11} className="animate-spin" /> : <Trash2 size={11} strokeWidth={1.5} />}
          </button>
        </div>
      )}
    </div>
  );
}
