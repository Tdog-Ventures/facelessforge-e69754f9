import React, { useEffect, useState, useMemo } from "react";
import { toast } from "sonner";
import { AudioLines, Loader2, Mic2 } from "lucide-react";
import { api, formatApiError } from "../lib/api";
import { useConfirm } from "./ConfirmDialog";
import VoiceStylePicker from "./voiceover/VoiceStylePicker";
import VoiceoverCard from "./voiceover/VoiceoverCard";
import SceneVoiceoverRow from "./voiceover/SceneVoiceoverRow";

export default function VoiceoverPanel({
  projectId, project, scenes = [], assets = [], hasScript, canEdit, onChange,
}) {
  const confirm = useConfirm();
  const [meta, setMeta] = useState({ mock: true, provider: "openai", model: "tts-1", default_voice_style: "narrator" });
  const [voiceStyle, setVoiceStyle] = useState(null);
  const [generatingFull, setGeneratingFull] = useState(false);
  const [generatingScene, setGeneratingScene] = useState(null);
  const [working, setWorking] = useState(null);

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

      {canEdit && <VoiceStylePicker value={voiceStyle} onChange={setVoiceStyle} />}

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
            .slice()
            .sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")))
            .map((v) => (
              <VoiceoverCard
                key={v.id}
                asset={v}
                label="Full script"
                isSelectedForProject={project?.selected_voiceover_asset_id === v.id}
                working={working}
                canEdit={canEdit}
                onSelect={select}
                onReject={reject}
                onDelete={remove}
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
              .map((scene) => (
                <SceneVoiceoverRow
                  key={scene.id}
                  scene={scene}
                  voiceovers={sceneVOMap[scene.id] || []}
                  generating={generatingScene === scene.id}
                  working={working}
                  canEdit={canEdit}
                  onGenerate={generateScene}
                  onSelect={select}
                  onReject={reject}
                  onDelete={remove}
                />
              ))}
          </div>
        )}
      </section>
    </div>
  );
}
