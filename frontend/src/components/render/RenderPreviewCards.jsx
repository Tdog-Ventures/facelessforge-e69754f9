import React from "react";

function fmtSec(s) {
  if (!s && s !== 0) return "—";
  const m = Math.floor(s / 60);
  const r = Math.round(s % 60);
  return m > 0 ? `${m}m ${String(r).padStart(2, "0")}s` : `${r}s`;
}

export default function RenderPreviewCards({ selectedThumbnail, selectedVoice }) {
  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
      <div className="border border-zinc-800 bg-[#121212] rounded-sm overflow-hidden">
        <div className="px-4 py-3 border-b border-zinc-800 font-mono text-[10px] uppercase tracking-widest text-zinc-500">
          Intro · selected thumbnail
        </div>
        {selectedThumbnail ? (
          <img
            src={selectedThumbnail.preview_url}
            alt="thumbnail"
            data-testid="render-preview-thumbnail"
            className="w-full aspect-video object-cover"
          />
        ) : (
          <div className="aspect-video flex items-center justify-center text-zinc-500 font-mono text-xs">
            No selected thumbnail
          </div>
        )}
      </div>
      <div className="border border-zinc-800 bg-[#121212] rounded-sm overflow-hidden">
        <div className="px-4 py-3 border-b border-zinc-800 font-mono text-[10px] uppercase tracking-widest text-zinc-500">
          Audio bed · voiceover
        </div>
        {selectedVoice ? (
          <div className="p-4 space-y-3">
            <div className="flex items-center gap-2">
              <span className="font-mono text-[10px] uppercase tracking-widest text-[#FF66CC]">
                {selectedVoice.voice_style}
              </span>
              <span className="font-mono text-[10px] text-zinc-500">
                {fmtSec(selectedVoice.duration)}
              </span>
            </div>
            <audio
              data-testid="render-preview-voiceover"
              controls
              src={selectedVoice.preview_url}
              preload="metadata"
              className="w-full h-10"
            />
          </div>
        ) : (
          <div className="aspect-video flex items-center justify-center text-zinc-500 font-mono text-xs">
            No selected full-script voiceover (will use scene-level audio if available)
          </div>
        )}
      </div>
    </div>
  );
}
