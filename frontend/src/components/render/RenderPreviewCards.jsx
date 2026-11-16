import React, { useState } from "react";
import { videoRatio } from "./RenderVideo";

const FRAME_ASPECT = { "9:16": "9 / 16", "1:1": "1 / 1", "16:9": "16 / 9" };

export function thumbnailObjectFit(width, height) {
  const w = Number(width);
  const h = Number(height);
  if (!(w > 0 && h > 0)) return "contain";
  return h >= w ? "cover" : "contain";
}

function fmtSec(s) {
  if (!s && s !== 0) return "—";
  const m = Math.floor(s / 60);
  const r = Math.round(s % 60);
  return m > 0 ? `${m}m ${String(r).padStart(2, "0")}s` : `${r}s`;
}

export default function RenderPreviewCards({ selectedThumbnail, selectedVoice, aspect }) {
  const [measured, setMeasured] = useState(null);
  const measuredFor = measured?.src === selectedThumbnail?.preview_url ? measured : null;
  const fit = thumbnailObjectFit(
    selectedThumbnail?.width || measuredFor?.width,
    selectedThumbnail?.height || measuredFor?.height,
  );
  const frameAspect = FRAME_ASPECT[aspect] || FRAME_ASPECT["9:16"];
  const frameStyle = {
    aspectRatio: frameAspect,
    width: "100%",
    maxWidth: `${40 * videoRatio(aspect)}vh`,
  };

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
      <div className="border border-[var(--ff-border)] bg-[var(--ff-surface)] rounded-sm overflow-hidden">
        <div className="px-4 py-3 border-b border-[var(--ff-border)] font-mono text-[10px] uppercase tracking-widest text-[var(--ff-text-muted)]">
          Intro · selected thumbnail
        </div>
        {selectedThumbnail ? (
          <div
            data-testid="render-preview-frame"
            className="relative mx-auto overflow-hidden bg-[#141820]"
            style={frameStyle}
          >
            {fit === "contain" && (
              <img
                src={selectedThumbnail.preview_url}
                alt=""
                aria-hidden="true"
                className="pointer-events-none absolute inset-0 h-full w-full scale-110 object-cover opacity-40 blur-xl"
              />
            )}
            <img
              src={selectedThumbnail.preview_url}
              alt="thumbnail"
              data-testid="render-preview-thumbnail"
              onLoad={(event) => {
                const { naturalWidth, naturalHeight } = event.currentTarget;
                if (naturalWidth && naturalHeight) {
                  setMeasured({
                    src: selectedThumbnail.preview_url,
                    width: naturalWidth,
                    height: naturalHeight,
                  });
                }
              }}
              className={`relative z-[1] block h-full w-full ${fit === "contain" ? "object-contain" : "object-cover"}`}
            />
          </div>
        ) : (
          <div className="aspect-video flex items-center justify-center text-[var(--ff-text-muted)] font-mono text-xs">
            No selected thumbnail
          </div>
        )}
      </div>
      <div className="border border-[var(--ff-border)] bg-[var(--ff-surface)] rounded-sm overflow-hidden">
        <div className="px-4 py-3 border-b border-[var(--ff-border)] font-mono text-[10px] uppercase tracking-widest text-[var(--ff-text-muted)]">
          Audio bed · voiceover
        </div>
        {selectedVoice ? (
          <div className="p-4 space-y-3">
            <div className="flex items-center gap-2">
              <span className="font-mono text-[10px] uppercase tracking-widest text-[#FF66CC]">
                {selectedVoice.voice_style}
              </span>
              <span className="font-mono text-[10px] text-[var(--ff-text-muted)]">
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
          <div className="aspect-video flex items-center justify-center text-[var(--ff-text-muted)] font-mono text-xs">
            No selected full-script voiceover (will use scene-level audio if available)
          </div>
        )}
      </div>
    </div>
  );
}
