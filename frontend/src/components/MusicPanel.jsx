import React, { useRef, useState } from "react";
import { toast } from "sonner";
import { Music, Upload, X, Loader2, Play } from "lucide-react";
import { api, formatApiError, UNBOUNDED_TIMEOUT } from "../lib/api";

export default function MusicPanel({ projectId, project, assets, canEdit, onProjectChange }) {
  const [uploading, setUploading] = useState(false);
  const [working, setWorking] = useState(false);
  const [rights, setRights] = useState(false);
  const [rightsNote, setRightsNote] = useState("");
  const [gain, setGain] = useState(project?.music_gain_db ?? -14);
  const [ducking, setDucking] = useState(project?.music_ducking_enabled ?? true);
  const fileRef = useRef(null);

  const musicAsset = (assets || []).find(
    (a) => a.id === project?.selected_music_asset_id && a.asset_type === "music_audio"
  );
  const enabled = !!project?.music_enabled && !!musicAsset;

  const refreshProject = async () => {
    const { data } = await api.get(`/projects/${projectId}`);
    onProjectChange(data);
  };

  const upload = async (file) => {
    if (!file) return;
    setUploading(true);
    try {
      const form = new FormData();
      form.append("file", file);
      form.append("rights_confirmed", rights ? "true" : "false");
      form.append("rights_note", rightsNote);
      await api.post(`/projects/${projectId}/music/upload`, form, {
        headers: { "Content-Type": "multipart/form-data" },
        ...UNBOUNDED_TIMEOUT,
      });
      toast.success(rights ? "Music uploaded and selected" : "Music uploaded — confirm rights to select it");
      setRights(false);
      setRightsNote("");
      await refreshProject();
    } catch (e) {
      toast.error("Music upload failed", { description: formatApiError(e.response?.data?.detail) || e.message });
    } finally {
      setUploading(false);
    }
  };

  const select = async () => {
    setWorking(true);
    try {
      await api.post(`/projects/${projectId}/music/${musicAsset.id}/select`, {
        music_gain_db: gain,
        music_ducking_enabled: ducking,
        rights_confirmed: rights,
        rights_note: rightsNote || undefined,
      });
      toast.success("Music selection saved");
      await refreshProject();
    } catch (e) {
      toast.error("Music select failed", { description: formatApiError(e.response?.data?.detail) || e.message });
    } finally {
      setWorking(false);
    }
  };

  const remove = async () => {
    setWorking(true);
    try {
      await api.delete(`/projects/${projectId}/music`);
      toast.success("Music disabled");
      await refreshProject();
    } catch (e) {
      toast.error("Remove failed", { description: formatApiError(e.response?.data?.detail) || e.message });
    } finally {
      setWorking(false);
    }
  };

  return (
    <div data-testid="music-panel" className="border-t border-[var(--ff-border)] pt-4 mt-4">
      <div className="font-mono text-[10px] uppercase tracking-widest text-[var(--ff-text-muted)] mb-1 flex items-center gap-2">
        <Music size={12} /> Background music
      </div>
      <p data-testid="music-state" className="font-mono text-[11px] text-[var(--ff-text-muted)] mb-3">
        {enabled
          ? `On · ${musicAsset.name || "track"} · ${project.music_gain_db ?? gain} dB · ducking ${project.music_ducking_enabled === false ? "off" : "on"}`
          : "Off · narrated videos render voice-only unless a track is selected"}
      </p>

      {enabled && (
        <div className="bg-[var(--ff-input-bg)] border border-[var(--ff-border)] rounded-sm p-3 mb-3 space-y-2">
          <div className="flex items-center gap-2">
            <span className="text-sm text-[var(--ff-text)] flex-1 truncate">{musicAsset.name}</span>
            {canEdit && (
              <button data-testid="music-remove-btn" onClick={remove} disabled={working}
                className="font-mono text-[10px] uppercase tracking-widest text-[var(--ff-text-muted)] hover:text-[#FF3366] border border-[var(--ff-border)] px-2 py-1 rounded-sm disabled:opacity-50">
                <span className="inline-flex items-center gap-1"><X size={12} /> Remove</span>
              </button>
            )}
          </div>
          {musicAsset.preview_url && (
            <audio data-testid="music-preview" controls src={musicAsset.preview_url} preload="metadata" className="w-full h-8" />
          )}
          <div className="font-mono text-[10px] text-[var(--ff-text-muted)]">
            Rights: {musicAsset.rights_note || (musicAsset.rights_confirmed ? "confirmed" : "unconfirmed")}
          </div>
        </div>
      )}

      {canEdit && (
        <div className="space-y-3">
          <div className="flex items-center gap-2 flex-wrap">
            <input ref={fileRef} type="file" accept=".mp3,.wav,.m4a,.ogg,.flac,audio/*" className="hidden"
              onChange={(e) => { upload(e.target.files?.[0]); e.target.value = ""; }} />
            <button data-testid="music-upload-btn" onClick={() => fileRef.current?.click()} disabled={uploading}
              className="flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-widest text-[var(--ff-text)] border border-[var(--ff-input-border)] px-3 py-2 rounded-sm hover:border-[#00E5FF] disabled:opacity-50">
              {uploading ? <Loader2 size={12} className="animate-spin" /> : <Upload size={12} />}
              {uploading ? "Uploading…" : musicAsset ? "Change track" : "Upload track"}
            </button>
            {enabled && (
              <button data-testid="music-save-btn" onClick={select} disabled={working}
                className="font-mono text-[10px] uppercase tracking-widest bg-[#00E5FF] text-black px-3 py-2 rounded-sm hover:bg-[#33EFFF] disabled:opacity-50">
                {working ? "Saving…" : "Save music settings"}
              </button>
            )}
          </div>
          <label className="flex items-center gap-2 text-sm text-[var(--ff-text)]">
            <input type="checkbox" checked={ducking} onChange={(e) => setDucking(e.target.checked)}
              className="h-4 w-4 accent-[#00E5FF] bg-[var(--ff-input-bg)] border-[var(--ff-input-border)] rounded" />
            Dynamic ducking under narration
            <span className="font-mono text-[10px] text-[var(--ff-text-dim)]">(default on)</span>
          </label>
          <div>
            <label className="font-mono text-[10px] uppercase tracking-widest text-[var(--ff-text-muted)]">
              Music level: {gain} dB (safe range −30…−6)
            </label>
            <input data-testid="music-gain-slider" type="range" min={-30} max={-6} step={1} value={gain}
              onChange={(e) => setGain(Number(e.target.value))} className="w-full accent-[#00E5FF]" />
          </div>
          <label className="flex items-start gap-2 text-sm text-[var(--ff-text)]">
            <input data-testid="music-rights-checkbox" type="checkbox" checked={rights}
              onChange={(e) => setRights(e.target.checked)}
              className="h-4 w-4 mt-0.5 accent-[#00E5FF] bg-[var(--ff-input-bg)] border-[var(--ff-input-border)] rounded" />
            <span>I confirm I have the rights to use this track in this video.
              <span className="block font-mono text-[10px] text-[var(--ff-text-dim)]">Required before a render will use uploaded music.</span>
            </span>
          </label>
          <input data-testid="music-rights-note" value={rightsNote} onChange={(e) => setRightsNote(e.target.value)}
            placeholder="Rights note, e.g. licensed from … / test fixture" maxLength={200}
            className="w-full bg-[var(--ff-input-bg)] border border-[var(--ff-border)] px-3 py-2 text-sm rounded-sm focus:border-[#00E5FF] text-[var(--ff-text)] placeholder:text-[var(--ff-input-placeholder)]" />
        </div>
      )}
      {!canEdit && !enabled && (
        <p className="font-mono text-[10px] text-[var(--ff-text-dim)] flex items-center gap-1"><Play size={12} /> No music on this project.</p>
      )}
    </div>
  );
}
