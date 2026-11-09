import React from "react";
import { qualityColor, qualityLabel } from "../lib/format";

export default function QualityScore({ score = 0, compact = false }) {
  const color = qualityColor(score);
  const label = qualityLabel(score);
  return (
    <div data-testid="quality-score" className={compact ? "space-y-1" : "space-y-2"}>
      <div className="flex items-baseline justify-between">
        <span className="font-mono text-[10px] uppercase tracking-widest text-[var(--ff-text-muted)]">
          Quality Score
        </span>
        <span className="font-mono text-[10px]" style={{ color }}>
          {label}
        </span>
      </div>
      <div className="flex items-center gap-3">
        <div className="font-mono tabular-nums font-semibold" style={{ color, fontSize: compact ? 20 : 28 }}>
          {score}
          <span className="text-[var(--ff-text-dim)] text-base">/100</span>
        </div>
        <div className="ff-progress flex-1">
          <span style={{ width: `${score}%`, background: color }} />
        </div>
      </div>
    </div>
  );
}
