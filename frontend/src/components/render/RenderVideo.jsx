import React, { useState } from "react";

const ASPECT_RATIOS = { "9:16": [9, 16], "1:1": [1, 1], "16:9": [16, 9] };

export function videoRatio(aspect) {
  const [width, height] = ASPECT_RATIOS[aspect] || ASPECT_RATIOS["9:16"];
  return width / height;
}

export default function RenderVideo({ src, aspect, poster, onTimeUpdate, ...props }) {
  const [metadata, setMetadata] = useState(null);
  const [width, height] = metadata && metadata.src === src
    ? [metadata.width, metadata.height]
    : ASPECT_RATIOS[aspect] || ASPECT_RATIOS["9:16"];
  const ratio = width / height;

  return (
    <video
      {...props}
      controls
      playsInline
      preload="metadata"
      src={src}
      poster={ratio > 1 ? poster : undefined}
      onTimeUpdate={onTimeUpdate}
      onLoadedMetadata={({ currentTarget: { videoWidth, videoHeight } }) => {
        if (videoWidth && videoHeight) setMetadata({ src, width: videoWidth, height: videoHeight });
      }}
      className="block mx-auto rounded-sm border border-[var(--ff-border)] bg-black"
      style={{ aspectRatio: `${width} / ${height}`, width: "100%", maxWidth: `${70 * ratio}vh`, objectFit: "contain" }}
    />
  );
}
