import React, { useEffect, useState, useCallback } from "react";
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle,
} from "./ui/dialog";
import { Loader2, Search, Film, Image as ImageIcon, Check, ExternalLink, Plus, AlertTriangle } from "lucide-react";
import { toast } from "sonner";
import { api, formatApiError } from "../lib/api";

const TABS = [
  { id: "both", label: "All", testId: "stock-tab-all" },
  { id: "videos", label: "Videos", testId: "stock-tab-videos" },
  { id: "photos", label: "Photos", testId: "stock-tab-photos" },
];

export default function StockAssetModal({ open, onOpenChange, projectId, scene, onAttached }) {
  const [query, setQuery] = useState("");
  const [mediaType, setMediaType] = useState("both");
  const [loading, setLoading] = useState(false);
  const [results, setResults] = useState([]);
  const [source, setSource] = useState(null);
  const [mock, setMock] = useState(false);
  const [warning, setWarning] = useState("");
  const [attaching, setAttaching] = useState(null); // external_id
  const [attached, setAttached] = useState(new Set());
  const [lastQuery, setLastQuery] = useState("");

  const search = useCallback(async (body) => {
    setLoading(true);
    setWarning("");
    try {
      const url = scene
        ? `/projects/${projectId}/scenes/${scene.id}/find-assets`
        : `/projects/${projectId}/stock-search`;
      const { data } = await api.post(url, body);
      setResults(data.results || []);
      setSource(data.source);
      setMock(!!data.mock);
      setLastQuery(data.query || "");
      if (data.warning) setWarning(data.warning);
    } catch (err) {
      toast.error("Search failed", { description: formatApiError(err.response?.data?.detail) || err.message });
    } finally {
      setLoading(false);
    }
  }, [projectId, scene]);

  // Auto-search on open using scene defaults
  useEffect(() => {
    if (open) {
      setAttached(new Set());
      setResults([]);
      setSource(null);
      setWarning("");
      // Use the scene's visual description for stock search, not the narration text.
      // Fall back to search_terms if visual_direction is empty.
      const toKeywords = (text = "") =>
        text
          .trim()
          .split(/\s+/)
          .slice(0, 8)
          .join(" ");
      const visual = scene?.visual_direction || "";
      const defaultQuery = visual
        ? toKeywords(visual)
        : (scene?.search_terms || [])
            .slice(0, 3)
            .map((t) => t.trim().split(/\s+/).slice(0, 3).join(" "))
            .filter(Boolean)
            .join(" ");
      setQuery(defaultQuery);
      search({ media_type: mediaType, per_page: 12, query: defaultQuery || undefined });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const runSearch = (e) => {
    e?.preventDefault?.();
    search({ media_type: mediaType, per_page: 12, query: query || undefined });
  };

  const switchType = (t) => {
    setMediaType(t);
    search({ media_type: t, per_page: 12, query: query || undefined });
  };

  const attach = async (item) => {
    if (!scene) {
      toast.error("Select a scene first");
      return;
    }
    setAttaching(item.external_id);
    try {
      const { data } = await api.post(
        `/projects/${projectId}/scenes/${scene.id}/attach-asset`,
        item,
      );
      toast.success(`Attached ${item.title}`);
      setAttached((prev) => new Set([...prev, item.external_id]));
      onAttached?.(data);
    } catch (err) {
      const code = err.response?.status;
      if (code === 409) {
        setAttached((prev) => new Set([...prev, item.external_id]));
        toast.info("Already attached to this scene");
      } else {
        toast.error("Attach failed", { description: formatApiError(err.response?.data?.detail) || err.message });
      }
    } finally {
      setAttaching(null);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        data-testid="stock-asset-modal"
        className="bg-[#0A0A0A] border border-zinc-800 rounded-sm max-w-5xl max-h-[85vh] overflow-hidden p-0 flex flex-col"
      >
        <DialogHeader className="px-6 pt-5 pb-4 border-b border-zinc-800 space-y-2">
          <div className="flex items-center justify-between gap-4">
            <div>
              <DialogTitle className="text-white text-lg font-semibold tracking-tight flex items-center gap-3">
                Find Stock Assets
                {mock && (
                  <span
                    data-testid="mock-mode-badge"
                    className="font-mono text-[10px] uppercase tracking-widest text-[#FFB020] border border-[#FFB020]/30 bg-[#FFB020]/10 px-2 py-0.5 rounded-sm"
                  >
                    Mock results
                  </span>
                )}
                {source === "pexels" && (
                  <span className="font-mono text-[10px] uppercase tracking-widest text-[#00FF66] border border-[#00FF66]/30 bg-[#00FF66]/10 px-2 py-0.5 rounded-sm">
                    Pexels · live
                  </span>
                )}
              </DialogTitle>
              {scene && (
                <DialogDescription className="text-zinc-400 text-xs font-mono uppercase tracking-widest mt-1">
                  Scene {String(scene.scene_number).padStart(2, "0")} · attach suggestions
                </DialogDescription>
              )}
            </div>
          </div>

          <form onSubmit={runSearch} className="flex items-center gap-2">
            <div className="relative flex-1">
              <Search size={14} strokeWidth={1.5} className="absolute left-3 top-1/2 -translate-y-1/2 text-zinc-500" />
              <input
                data-testid="stock-search-input"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder={scene?.search_terms?.slice(0, 2).join(" ") || "Search Pexels…"}
                className="w-full bg-[#121212] border border-zinc-800 pl-9 pr-3 py-2 text-sm rounded-sm focus:border-[#00E5FF]"
              />
            </div>
            <div className="flex border border-zinc-800 rounded-sm overflow-hidden">
              {TABS.map((t) => (
                <button
                  key={t.id}
                  type="button"
                  data-testid={t.testId}
                  onClick={() => switchType(t.id)}
                  className={`px-3 py-2 font-mono text-[10px] uppercase tracking-widest transition-colors ${
                    mediaType === t.id ? "bg-[#00E5FF] text-black" : "text-zinc-400 hover:text-white hover:bg-[#1A1A1A]"
                  }`}
                >
                  {t.label}
                </button>
              ))}
            </div>
            <button
              data-testid="stock-search-btn"
              type="submit"
              disabled={loading}
              className="flex items-center gap-2 bg-[#00E5FF] text-black font-semibold text-sm px-4 py-2 rounded-sm hover:bg-[#33EFFF] disabled:opacity-60 transition-colors"
            >
              {loading ? <Loader2 size={14} className="animate-spin" /> : <Search size={14} strokeWidth={2} />}
              Search
            </button>
          </form>

          {warning && (
            <div className="flex items-center gap-2 text-[#FFB020] text-xs font-mono pt-1">
              <AlertTriangle size={12} strokeWidth={1.5} /> {warning}
            </div>
          )}
          {lastQuery && !warning && (
            <div className="font-mono text-[10px] text-zinc-500 uppercase tracking-widest pt-1">
              Query · <span className="text-[#00E5FF] normal-case tracking-normal">{lastQuery}</span>
            </div>
          )}
        </DialogHeader>

        <div className="flex-1 overflow-y-auto p-6">
          {loading ? (
            <div className="h-full flex items-center justify-center text-sm text-zinc-500 font-mono">
              <Loader2 className="animate-spin mr-2" size={14} /> Searching…
            </div>
          ) : results.length === 0 ? (
            <div className="h-full min-h-[280px] flex flex-col items-center justify-center text-zinc-400 text-sm gap-3">
              <Search size={32} strokeWidth={1} className="text-zinc-700" />
              No results yet. Try a broader query like "neon city night" or "data center".
            </div>
          ) : (
            <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-4 gap-3">
              {results.map((r) => {
                const isAttached = attached.has(r.external_id);
                const isAttaching = attaching === r.external_id;
                return (
                  <div
                    key={`${r.source}-${r.external_id}`}
                    data-testid={`stock-result-${r.external_id}`}
                    className="border border-zinc-800 bg-[#121212] rounded-sm overflow-hidden group"
                  >
                    <div className="relative aspect-video bg-[#1A1A1A] overflow-hidden">
                      {r.preview_url ? (
                        <img
                          src={r.preview_url}
                          alt={r.title}
                          className="w-full h-full object-cover"
                          loading="lazy"
                          referrerPolicy="no-referrer"
                        />
                      ) : (
                        <div className="w-full h-full flex items-center justify-center text-zinc-700">
                          {r.media_type === "stock_video" ? <Film size={24} /> : <ImageIcon size={24} />}
                        </div>
                      )}
                      <span
                        className="absolute top-2 left-2 font-mono text-[9px] uppercase tracking-widest px-1.5 py-0.5 rounded-sm border"
                        style={{
                          color: r.media_type === "stock_video" ? "#7B61FF" : "#00E5FF",
                          background: r.media_type === "stock_video" ? "rgba(123,97,255,0.1)" : "rgba(0,229,255,0.1)",
                          borderColor: r.media_type === "stock_video" ? "rgba(123,97,255,0.3)" : "rgba(0,229,255,0.3)",
                        }}
                      >
                        {r.media_type === "stock_video" ? "Video" : "Photo"}
                      </span>
                      {r.duration ? (
                        <span className="absolute top-2 right-2 font-mono text-[9px] text-white bg-black/70 px-1.5 py-0.5 rounded-sm">
                          {r.duration}s
                        </span>
                      ) : null}
                    </div>
                    <div className="p-3 space-y-2">
                      <div className="text-xs text-zinc-200 truncate" title={r.title}>{r.title}</div>
                      <div className="font-mono text-[10px] text-zinc-500 uppercase tracking-widest truncate">
                        by <span className="text-[#7B61FF]">{r.attribution_name}</span>
                        <span className="text-zinc-600"> · {r.width}×{r.height}</span>
                      </div>
                      <div className="flex items-center gap-2 pt-1">
                        <button
                          data-testid={`attach-btn-${r.external_id}`}
                          disabled={isAttached || isAttaching || !scene}
                          onClick={() => attach(r)}
                          className={`flex-1 flex items-center justify-center gap-1.5 font-mono text-[10px] uppercase tracking-widest px-2 py-1.5 rounded-sm transition-colors ${
                            isAttached
                              ? "bg-[#00FF66]/10 text-[#00FF66] border border-[#00FF66]/30"
                              : "bg-[#00E5FF] text-black hover:bg-[#33EFFF] disabled:opacity-60"
                          }`}
                        >
                          {isAttaching ? <Loader2 size={11} className="animate-spin" /> :
                            isAttached ? <Check size={11} strokeWidth={2} /> : <Plus size={11} strokeWidth={2} />}
                          {isAttached ? "Attached" : isAttaching ? "Attaching" : "Attach"}
                        </button>
                        {r.source_url && (
                          <a
                            href={r.source_url}
                            target="_blank"
                            rel="noreferrer"
                            className="p-1.5 border border-zinc-800 text-zinc-400 hover:text-[#00E5FF] hover:border-[#00E5FF] rounded-sm transition-colors"
                            title="Open source"
                          >
                            <ExternalLink size={12} strokeWidth={1.5} />
                          </a>
                        )}
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        <div className="px-6 py-3 border-t border-zinc-800 flex items-center justify-between">
          <div className="font-mono text-[10px] text-zinc-500 uppercase tracking-widest">
            {attached.size > 0 ? `${attached.size} attached · ` : ""}
            {mock ? "Live search will use Pexels once PEXELS_API_KEY is set." : "Source · Pexels"}
          </div>
          <button
            data-testid="stock-modal-close"
            onClick={() => onOpenChange(false)}
            className="text-sm text-zinc-400 hover:text-white transition-colors px-3 py-1.5"
          >
            Done
          </button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
