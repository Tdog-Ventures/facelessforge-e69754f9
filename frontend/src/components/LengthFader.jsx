import React from "react";
import { RENDER_CREDIT_COST } from "../lib/credits";
import { formatLength, lengthIndex, lengthStops } from "../lib/length";

export default function LengthFader({
  value,
  onChange,
  id,
  testId = "field-duration",
  describedBy,
  invalid,
}) {
  const stops = lengthStops();
  const index = lengthIndex(value);
  const shown = stops[index];
  const label = formatLength(shown);

  return (
    <div className="space-y-3">
      <div data-testid="length-readout" className="font-mono text-2xl tracking-tight text-[var(--ff-text)]">
        {label}
      </div>
      <input
        id={id}
        data-testid={testId}
        type="range"
        min={0}
        max={stops.length - 1}
        step={1}
        value={index}
        aria-valuemin={0}
        aria-valuemax={stops.length - 1}
        aria-valuenow={index}
        aria-valuetext={label}
        aria-invalid={invalid ? "true" : undefined}
        aria-describedby={describedBy}
        onChange={(e) => {
          const next = Math.min(stops.length - 1, Math.max(0, Number(e.target.value)));
          onChange(stops[next]);
        }}
        className="ff-length"
      />
      <div className="flex items-center justify-between font-mono text-[10px] uppercase tracking-widest text-[var(--ff-text-muted)]">
        <span>5 seconds</span>
        <span>60 minutes</span>
      </div>
      <p className="font-mono text-[10px] leading-relaxed text-[var(--ff-text-dim)]">
        Under a minute the fader moves in 5-second steps. From 1 minute on, it moves in whole minutes.
        A render costs {RENDER_CREDIT_COST} credits at any length.
      </p>
    </div>
  );
}
