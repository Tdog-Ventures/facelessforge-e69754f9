import React, { useEffect, useState, useMemo } from "react";
import { toast } from "sonner";
import {
  AudioLines, Loader2, Star, X as XIcon, Trash2, Download, Mic2,
} from "lucide-react";
import { api, formatApiError } from "../lib/api";
import { useConfirm } from "./ConfirmDialog";

const VOICE_STYLES = [
  { id: "narrator",    label: "Narrator",    hint: "Deep, low, male" },
  { id: "energetic",   label: "Energetic",   hint: "Bright, upbeat" },
  { id: "documentary", label: "Documentary", hint: "Neutral, measured" },
  { id: "calm",        label: "Calm",        hint: "Warm, smooth" },
  { id: "dramatic",    label: "Dramatic",    hint: "Theatrical, bold" },
  { id: "corporate",   label: "Corporate",   hint: "Professional, even" },
  { id: "mysterious",  label: "Mysterious",  hint: "Breathy, intimate" },
];

function fmtSec(s) {
  if (!s && s !== 0) return "—";
  const m = Math.floor(s / 60);
  const r = Math.round(s % 60);
  return m > 0 ? `${m}m ${String(r).padStart(2, "0")}s` : `${r}s`;
}

export default function VoiceoverPanel({
  projectId, project, scenes = [], assets = [], hasScript, canEdit, onChange,
}) {
  const confirm = useConfirm();
  const [meta, setMeta] = useState({ mock: true, provider: "openai", model: "tts-1", default_voice_style: "narrator" });
  const [voiceStyle, setVoiceStyle] = useState(null);
  const [generatingFull, setGeneratingFull] = useState(false);
  const [generatingScene, setGeneratingScene] = useState(null); // scene id
  const [working, setWorking] = useState(null); // asset id

  useEffect(() => {
    (async () => {
      try {
        const { data } = await api.get("/tts/meta");
        setMeta(data);
        if (!voiceStyle) setVoiceStyle(data.default_voice_style || "narrator");
      } catch {/* noop */}
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const voiceovers = useMemo(
    () => (assets || []).filter((a) => a.asset_type === "voiceover_audio"),
    [assets],
  );
  const fullScriptVOs = voiceovers.filter((v) => !v.scene_id);
  const sceneVOs = voiceovers.filter((v) => v.scene_id);
  const sceneVOMap = useMemo(() => {
    const m = {};
    sceneVOs.forEach((v) => {
      const list = m[v.scene_id] || (m[v.scene_id] = []);
      list.push(v);
    });
    Object.values(m).forEach((arr) => arr.sort((a, b) =>
      String(b.created_at || "").localeCompare(String(a.created_at || ""))
    ));
    return m;
  }, [sceneVOs]);

  const generateFull = async () => {
    if (!hasScript) {
      toast.error("Generate the script first");
      return;
    }
    setGeneratingFull(true);
    const tid = toast.loading("Generating full-script voiceover…");
    try {
      const { data } = await api.post(
        `/projects/${projectId}/voiceover/generate-script`,
        { voice_style: voiceStyle },
      );
      onChange(data);
      toast.success(meta.mock ? "Mock voiceover ready" : "Voiceover generated", { id: tid });
    } catch (err) {
      toast.error("Voiceover failed", {
        id: tid,
        description: formatApiError(err.response?.data?.detail) || err.message,
      });
    } finally {
      setGeneratingFull(false);
    }
  };

  const generateScene = async (scene) => {
    if (!scene.narration_text) {
      toast.error("Scene has no narration text");
      return;
    }
    setGeneratingScene(scene.id);
    const tid = toast.loading(`Generating scene ${scene.scene_number} voiceover…`);
    try {
      const { data } = await api.post(
        `/projects/${projectId}/scenes/${scene.id}/voiceover/generate`,
        { voice_style: voiceStyle },
      );
      onChange(data);
      toast.success("Scene voiceover ready", { id: tid });
    } catch (err) {
      toast.error("Scene voiceover failed", {
        id: tid,
        description: formatApiError(err.response?.data?.detail) || err.message,
      });
    } finally {
      setGeneratingScene(null);
    }
  };

  const select = async (asset) => {
    setWorking(asset.id);
    try {
      const { data } = await api.post(`/projects/${projectId}/voiceover/${asset.id}/select`);
      onChange(data);
      toast.success("Selected");
    } catch (err) {
      toast.error("Select failed", { description: formatApiError(err.response?.data?.detail) || err.message });
    } finally {
      setWorking(null);
    }
  };

  const reject = async (asset) => {
    setWorking(asset.id);
    try {
      const { data } = await api.post(`/projects/${projectId}/voiceover/${asset.id}/reject`);
      onChange(data);
      toast.success("Rejected");
    } catch (err) {
      toast.error("Reject failed", { description: formatApiError(err.response?.data?.detail) || err.message });
    } finally {
      setWorking(null);
    }
  };

  const remove = async (asset) => {
    const ok = await confirm({
      title: "Delete this voiceover?",
      description: "The audio file will be permanently removed.",
      confirmLabel: "Delete",
      tone: "destructive",
    });
    if (!ok) return;
    setWorking(asset.id);
    try {
      const { data } = await api.delete(`/projects/${projectId}/voiceover/${asset.id}`);
      onChange(data);
      toast.success("Deleted");
    } catch (err) {
      toast.error("Delete failed", { description: formatApiError(err.response?.data?.detail) || err.message });
    } finally {
      setWorking(null);
    }
  };

  const Card = ({ asset, label, isSelectedForProject = false }) => {
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
                onClick={() => select(asset)}
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
                onClick={() => reject(asset)}
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
              onClick={() => remove(asset)}
              disabled={isWorking}
              className="flex items-center gap-1 font-mono text-[10px] uppercase tracking-widest text-zinc-500 hover:text-[#FF3366] hover:border-[#FF3366] border border-zinc-800 px-2 py-1 rounded-sm transition-colors disabled:opacity-50 ml-auto"
            >
              {isWorking ? <Loader2 size={11} className="animate-spin" /> : <Trash2 size={11} strokeWidth={1.5} />}
            </button>
          </div>
        )}
      </div>
    );
  };

  return (
    <div className="space-y-8">
      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <div className="font-mono text-[11px] uppercase tracking-widest text-zinc-500">
            {meta.provider} · {meta.model}
          </div>
          <h2 className="text-lg font-semibold text-white mt-1 flex items-center gap-2">
            <Mic2 size={16} className="text-[#7B61FF]" /> Voiceover
          </h2>
        </div>
        <div className="flex items-center gap-2">
          {meta.mock ? (
            <span
              data-testid="vo-mock-badge"
              className="font-mono text-[10px] uppercase tracking-widest text-[#FFB020] border border-[#FFB020]/30 bg-[#FFB020]/10 px-2 py-0.5 rounded-sm"
            >
              Mock TTS
            </span>
          ) : (
            <span
              data-testid="vo-live-badge"
              className="font-mono text-[10px] uppercase tracking-widest text-[#00FF66] border border-[#00FF66]/30 bg-[#00FF66]/10 px-2 py-0.5 rounded-sm"
            >
              OpenAI · Live
            </span>
          )}
        </div>
      </div>

      {/* Voice style picker */}
      {canEdit && (
        <div className="border border-zinc-800 bg-[#0F0F10] rounded-sm p-4 space-y-3">
          <div className="font-mono text-[10px] uppercase tracking-widest text-zinc-500">
            Voice style
          </div>
          <div className="flex flex-wrap gap-2" data-testid="vo-style-picker">
            {VOICE_STYLES.map((s) => (
              <button
                key={s.id}
                data-testid={`vo-style-${s.id}`}
                onClick={() => setVoiceStyle(s.id)}
                className={`px-3 py-1.5 font-mono text-[10px] uppercase tracking-widest rounded-sm border transition-colors ${
                  voiceStyle === s.id
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
      )}

      {/* Full-script section */}
      <section className="space-y-3">
        <div className="flex items-center justify-between flex-wrap gap-3">
          <div>
            <div className="font-mono text-[10px] uppercase tracking-widest text-zinc-500">
              Full script voiceover
            </div>
            <div className="text-sm text-zinc-300 mt-1">
              One audio for the entire video. Used as the master narration.
            </div>
          </div>
          {canEdit && (
            <button
              data-testid="vo-generate-full-btn"
              onClick={generateFull}
              disabled={generatingFull || !hasScript}
              className="flex items-center gap-2 font-mono text-[11px] uppercase tracking-widest bg-[#00E5FF] text-black hover:bg-[#33EFFF] disabled:opacity-50 px-4 py-2 rounded-sm transition-colors"
            >
              {generatingFull ? <Loader2 size={12} className="animate-spin" /> : <AudioLines size={12} />}
              {fullScriptVOs.length > 0 ? "Regenerate full" : "Generate full script"}
            </button>
          )}
        </div>
        {!hasScript && (
          <div className="border border-zinc-800 border-dashed p-6 text-center rounded-sm text-sm text-zinc-500">
            Generate the script first.
          </div>
        )}
        {hasScript && fullScriptVOs.length === 0 && (
          <div className="border border-zinc-800 border-dashed p-6 text-center rounded-sm text-sm text-zinc-500">
            No full-script voiceover yet.
          </div>
        )}
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          {fullScriptVOs
            .sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")))
            .map((v) => (
              <Card
                key={v.id}
                asset={v}
                label="Full script"
                isSelectedForProject={project?.selected_voiceover_asset_id === v.id}
              />
            ))}
        </div>
      </section>

      {/* Per-scene section */}
      <section className="space-y-3">
        <div className="flex items-center justify-between flex-wrap gap-3">
          <div>
            <div className="font-mono text-[10px] uppercase tracking-widest text-zinc-500">
              Scene voiceovers
            </div>
            <div className="text-sm text-zinc-300 mt-1">
              Per-scene narration — required for the future ffmpeg render queue.
            </div>
          </div>
          <div className="font-mono text-[10px] uppercase tracking-widest text-zinc-500">
            {sceneVOs.length}/{scenes.length} scenes have audio
          </div>
        </div>
        {scenes.length === 0 ? (
          <div className="border border-zinc-800 border-dashed p-6 text-center rounded-sm text-sm text-zinc-500">
            Generate scenes first.
          </div>
        ) : (
          <div className="space-y-3">
            {scenes
              .slice()
              .sort((a, b) => (a.scene_number || 0) - (b.scene_number || 0))
              .map((scene) => {
                const list = sceneVOMap[scene.id] || [];
                const generating = generatingScene === scene.id;
                return (
                  <div
                    key={scene.id}
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
                          onClick={() => generateScene(scene)}
                          disabled={generating || !scene.narration_text}
                          className="flex items-center gap-2 font-mono text-[10px] uppercase tracking-widest border border-[#7B61FF]/40 text-[#7B61FF] hover:bg-[#7B61FF]/10 disabled:opacity-50 px-3 py-1.5 rounded-sm transition-colors"
                        >
                          {generating ? <Loader2 size={11} className="animate-spin" /> : <AudioLines size={11} />}
                          {list.length > 0 ? "Regenerate" : "Generate"}
                        </button>
                      )}
                    </div>
                    {list.length > 0 && (
                      <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
                        {list.map((v) => (
                          <Card
                            key={v.id}
                            asset={v}
                            label={`Scene ${String(scene.scene_number).padStart(2, "0")}`}
                          />
                        ))}
                      </div>
                    )}
                  </div>
                );
              })}
          </div>
        )}
      </section>
    </div>
  );
}
