import { useState, useEffect, useMemo } from "react";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { X, Search, Film, Image as ImageIcon } from "lucide-react";
import { api, formatApiError } from "../lib/api";
import { toast } from "sonner";

const sourceLabel = { pexels: "PEXELS", pixabay: "PIXABAY", unsplash: "UNSPLASH", mixed: "MIXED" };
const sourceColor = { pexels: "bg-[#05A081]/20 text-[#05A081]", pixabay: "bg-[#00ABE7]/20 text-[#00ABE7]", unsplash: "bg-white/10 text-white", mixed: "bg-[#00FF88]/20 text-[#00FF88]" };

export default function StockAssetModal({ open, onOpenChange, projectId, scene, onAttached }) {
  const [query, setQuery] = useState("");
  const [mediaType, setMediaType] = useState("videos"); // videos|photos|both -> api: videos|photos|both
  const [results, setResults] = useState([]);
  const [source, setSource] = useState("mixed");
  const [loading, setLoading] = useState(false);
  const [attached, setAttached] = useState(new Set());
  const [searchTerms, setSearchTerms] = useState([]);

  function buildQuery(scene){
    const BAD=["stock b-roll matching the narration","b-roll matching","matching the narration","cinematic b-roll"];
    let q=(scene.visual_direction||"").trim();
    const isBad=!q || BAD.some(b=>q.toLowerCase().includes(b)) || q.length<25;
    if(isBad){ q=scene.description||scene.narration||scene.text||scene.voiceover_text||scene.topic||""; }
    return q.split(".")[0].slice(0,120).trim() || "cinematic b-roll";
  }

  useEffect(() => {
    if (open && scene) {
      const q = buildQuery(scene);
      setQuery(q);
      setSearchTerms(scene.search_terms || []);
      // auto search on open
      doSearch(q, mediaType);
    }
  }, [open, scene?.id]);

  const doSearch = async (q, type) => {
    const finalQuery = (q || query || "").trim().slice(0, 80) || buildQuery(scene);
    const mt = type || mediaType;
    setLoading(true);
    try {
      // POST /api/projects/{projectId}/scenes/{sceneId}/find-assets supports query, media_type, per_page
      const { data } = await api.post(`/projects/${projectId}/scenes/${scene.id}/find-assets`, {
        query: finalQuery,
        media_type: mt === "all" || mt === "both" ? "both" : mt,
        per_page: 24,
      });
      // data may be {results, source, mock} or array
      const res = Array.isArray(data) ? data : (data.results || data.items || []);
      const src = data.source || "mixed";
      setResults(res);
      setSource(src);
    } catch (err) {
      toast.error("Search failed", { description: formatApiError(err.response?.data?.detail) || err.message });
      setResults([]);
    } finally {
      setLoading(false);
    }
  };

  const handleSearch = (e) => {
    e?.preventDefault();
    doSearch(query, mediaType);
  };

  const handleAttach = async (r) => {
    try {
      // attach via POST /api/projects/{projectId}/scenes/{sceneId}/attach-asset
      const payload = {
        source: r.source || "pexels",
        external_id: r.external_id || r.id,
        media_type: r.media_type || (r.type === "photo" ? "stock_image" : "stock_video"),
        title: r.title || r.description || query,
        preview_url: r.preview_url || r.src?.medium || r.url,
        source_url: r.source_url || r.url,
        download_url: r.download_url || r.src?.original || r.url,
        attribution_name: r.attribution_name || r.photographer || r.author,
        attribution_url: r.attribution_url || "",
        width: r.width || 0,
        height: r.height || 0,
        duration: r.duration || null,
        tags: r.tags || [],
        query: query,
      };
      await api.post(`/projects/${projectId}/scenes/${scene.id}/attach-asset`, payload);
      setAttached(prev => new Set([...prev, r.external_id || r.id]));
      toast.success("Attached");
      onAttached?.();
    } catch (err) {
      toast.error("Attach failed", { description: formatApiError(err.response?.data?.detail) || err.message });
    }
  };

  const dedupedResults = useMemo(() => {
    const seen = new Set(attached);
    return (results || []).filter(r => {
      const key = r.external_id || r.id || r.preview_url;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }, [results, attached]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="w-[96vw] max-w-5xl max-h-[96vh] sm:max-h-[90vh] m-0 sm:m-auto overflow-hidden p-0 flex flex-col [&>button:last-child]:hidden bg-[#0A0A0A] border-[#1A1A1A]">
        <button onClick={() => onOpenChange(false)} className="absolute right-2 top-2 sm:right-4 sm:top-4 p-3 bg-black/80 rounded-full sm:rounded-sm backdrop-blur z-20 hover:bg-black">
          <X size={20} className="sm:w-[18px] text-white" />
        </button>
        <DialogHeader className="p-4 sm:p-6 pb-0 shrink-0">
          <div className="flex items-center gap-3">
            <DialogTitle className="text-white">Find Stock Assets</DialogTitle>
            <span className={`text-[10px] px-2 py-1 border ${sourceColor[source] || sourceColor.mixed}`}>{sourceLabel[source] || "MIXED"} · LIVE</span>
          </div>
          <div className="flex items-center gap-2 mt-1">
            <span className="text-[10px] text-white/50 tracking-[0.2em]">SCENE {scene?.scene_number || scene?.index || "02"}</span>
            <span className="text-white/20">·</span>
            <button
              onClick={() => {
                const el = document.getElementById(`vd-${scene?.id}`);
                if (el) el.classList.toggle("line-clamp-1");
              }}
              className="flex items-center gap-1 text-[11px] text-slate-400 hover:text-white truncate max-w-[60%] text-left"
              title={scene?.visual_direction || ""}
            >
              <span id={`vd-${scene?.id}`} className="line-clamp-1 truncate">{scene?.visual_direction || "—"}</span>
              <span className="text-[10px]">⌄</span>
            </button>
          </div>
          {/* Editable search terms chips */}
          {searchTerms.length > 0 && (
            <div className="mt-3 flex flex-wrap gap-1">
              {searchTerms.map(t => (
                <button key={t} onClick={() => { setQuery(t); doSearch(t, mediaType); }} className="text-[10px] px-2 py-1 border border-slate-700 text-slate-400 hover:text-white hover:border-blue-400 rounded-full">{t}</button>
              ))}
            </div>
          )}
        </DialogHeader>
        <form onSubmit={handleSearch} className="p-4 sm:p-6 pt-3 flex flex-col sm:flex-row items-stretch sm:items-center gap-2 shrink-0">
          <div className="relative flex-1 w-full">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-white/40" />
            <Input value={query} onChange={e => setQuery(e.target.value)} placeholder="Search Pexels, Pixabay, Unsplash… (e.g. Adelaide driveway pressure wash before after)" className="pl-9 py-3 sm:py-2 bg-[#141414] border-[#222] text-white w-full" />
          </div>
          <div className="flex gap-2 w-full sm:w-auto">
            <div className="flex flex-1 sm:flex-none bg-[#141414] border border-[#222] rounded">
              {[
                { key: "videos", label: "VIDEOS", icon: Film },
                { key: "photos", label: "PHOTOS", icon: ImageIcon },
                { key: "both", label: "ALL", icon: Search },
              ].map(t => (
                <button key={t.key} type="button" onClick={()=>{setMediaType(t.key); doSearch(query, t.key)}} className={`flex items-center gap-1 flex-1 sm:flex-none min-h-[44px] sm:min-h-0 px-3 py-3 sm:py-2 text-[11px] tracking-widest ${mediaType===t.key?"bg-cyan-400 text-black":"text-white/60 hover:text-white active:bg-[#1A1A1A]"}`}>
                  <t.icon size={12} />{t.label}
                </button>
              ))}
            </div>
            <Button type="submit" className="min-h-[44px] min-w-[64px] px-5 py-3 sm:py-2 bg-cyan-400 text-black hover:bg-cyan-300"><Search className="w-4 h-4 sm:mr-2" /><span className="hidden sm:inline">Search</span></Button>
          </div>
        </form>
        <div className="flex-1 overflow-y-auto p-3 sm:p-6 overscroll-contain touch-pan-y" style={{WebkitOverflowScrolling:'touch'}}>
          {loading? <div className="min-h-[40vh] flex items-center justify-center text-white/40">Searching {sourceLabel[source]}…</div> :
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
              {dedupedResults.length === 0 && !loading ? <div className="col-span-full text-center text-white/40 py-10">No results — try a different query or switch to ALL</div> : dedupedResults.map(r => (
                <div key={r.external_id || r.id} className="group bg-[#141414] border border-[#1E1E1E] overflow-hidden hover:border-[#2A2A2A] transition">
                  <div className="relative aspect-video overflow-hidden bg-black min-h-[200px] sm:min-h-[220px]">
                    {r.preview_url? <img src={r.preview_url} className="w-full h-full object-cover min-h-[200px]" loading="lazy" /> : <div className="w-full h-full bg-[#111] min-h-[200px]" />}
                    <div className="absolute inset-0 bg-gradient-to-t from-black/70 via-transparent to-transparent opacity-0 group-hover:opacity-100 transition-opacity" />
                    <div className="absolute top-2 right-2 flex gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                      <span className="text-[9px] px-1.5 py-0.5 bg-black/70 backdrop-blur text-cyan-300 border border-white/10">{r.media_type === "stock_image" ? "PHOTO" : "VIDEO"}</span>
                      <span className="text-[9px] px-1.5 py-0.5 bg-black/70 text-white/80">{(r.source||source).toUpperCase()}{r.duration?` · ${r.duration}s`:""}</span>
                    </div>
                    {attached.has(r.external_id) && <span className="absolute bottom-2 left-2 text-[8px] px-1.5 py-0.5 bg-amber-500/90 text-black">USED</span>}
                  </div>
                  <div className="p-3">
                    <div className="text-[13px] text-white/90 line-clamp-2 min-h-[36px]">{r.title || r.description || "Untitled"}</div>
                    <div className="text-[10px] text-white/40 mt-1 truncate">BY { (r.attribution_name||r.photographer||r.author||"UNKNOWN").toUpperCase()} · {r.width}×{r.height}</div>
                    <Button onClick={()=>handleAttach(r)} disabled={attached.has(r.external_id)} className="w-full mt-3 min-h-[40px] py-2.5 sm:py-1.5 bg-cyan-400 text-black hover:bg-cyan-300 disabled:opacity-30 active:scale-[0.98] transition-transform text-[11px] tracking-widest">{attached.has(r.external_id)?"ATTACHED":"ATTACH"}</Button>
                  </div>
                </div>
              ))}
            </div>
          }
        </div>
        <div className="shrink-0 bg-[#0A0A0A] px-4 sm:px-6 py-3 flex justify-between items-center border-t border-[#1A1A1A]" style={{paddingBottom:'max(0.75rem, env(safe-area-inset-bottom))'}}>
          <div className="text-[10px] text-white/30 tracking-widest">SOURCE · {source.toUpperCase()} · {dedupedResults.length} results</div>
          <Button variant="ghost" onClick={()=>onOpenChange(false)} className="min-h-[44px] px-4 text-white/60">Done</Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
