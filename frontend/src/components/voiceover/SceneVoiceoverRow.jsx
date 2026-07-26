import React from "react";
import { Loader2, AudioLines } from "lucide-react";
import VoiceoverCard from "./VoiceoverCard";

export default function SceneVoiceoverRow({
  scene, voiceovers, generating, working, canEdit,
  onGenerate, onSelect, onReject, onDelete,
}) {
  return (
    <div
      data-testid={`vo-scene-${scene.id}`}
      className="border border-zinc-800 bg-[#121212] rounded-sm p-4 space-y-3"
    >
      <div className="flex items-start justify-between gap-3 flex-wrap">
        <div className="flex-1 min-w-0">
          <div className="font-mono text-[10px] uppercase tracking-widest text-[#00E5FF] mb-1">
            Scene {String(scene.scene_number).padStart(2, "0")}
          </div>
          <p className="text-sm text-zinc-300 line-clamp-2 leading-relaxed">
            {scene.narration_text || <span className="text-zinc-500 italic">No narration text</span>}
          </p>
        </div>
        {canEdit && (
          <button
            data-testid={`vo-generate-scene-${scene.id}`}
            onClick={() => onGenerate(scene)}
            disabled={generating || !scene.narration_text}
            className="flex items-center gap-2 font-mono text-[10px] uppercase tracking-widest border border-[#7B61FF]/40 text-[#7B61FF] hover:bg-[#7B61FF]/10 disabled:opacity-50 px-3 py-1.5 rounded-sm transition-colors"
          >
            {generating ? <Loader2 size={11} className="animate-spin" /> : <AudioLines size={11} />}
            {voiceovers.length > 0 ? "Regenerate" : "Generate"}
          </button>
        )}
      </div>
      {voiceovers.length > 0 && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
          {voiceovers.map((v) => (
            <VoiceoverCard
              key={v.id}
              asset={v}
              label={`Scene ${String(scene.scene_number).padStart(2, "0")}`}
              working={working}
              canEdit={canEdit}
              onSelect={onSelect}
              onReject={onReject}
              onDelete={onDelete}
            />
          ))}
        </div>
      )}
    </div>
  );
}
