import React from "react";

export const VOICE_STYLES = [
  { id: "narrator",    label: "Narrator",    hint: "Deep, low, male" },
  { id: "energetic",   label: "Energetic",   hint: "Bright, upbeat" },
  { id: "documentary", label: "Documentary", hint: "Neutral, measured" },
  { id: "calm",        label: "Calm",        hint: "Warm, smooth" },
  { id: "dramatic",    label: "Dramatic",    hint: "Theatrical, bold" },
  { id: "corporate",   label: "Corporate",   hint: "Professional, even" },
  { id: "mysterious",  label: "Mysterious",  hint: "Breathy, intimate" },
];

export default function VoiceStylePicker({ value, onChange }) {
  return (
    <div className="border border-zinc-800 bg-[#0F0F10] rounded-sm p-4 space-y-3">
      <div className="font-mono text-[10px] uppercase tracking-widest text-zinc-500">
        Voice style
      </div>
      <div className="flex flex-wrap gap-2" data-testid="vo-style-picker">
        {VOICE_STYLES.map((s) => (
          <button
            key={s.id}
            data-testid={`vo-style-${s.id}`}
            onClick={() => onChange(s.id)}
            className={`px-3 py-1.5 font-mono text-[10px] uppercase tracking-widest rounded-sm border transition-colors ${
              value === s.id
                ? "bg-[#7B61FF] text-white border-[#7B61FF]"
                : "border-zinc-700 text-zinc-400 hover:text-white hover:border-zinc-500"
            }`}
            title={s.hint}
          >
            {s.label}
          </button>
        ))}
      </div>
    </div>
  );
}
