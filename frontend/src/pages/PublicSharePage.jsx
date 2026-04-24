import React, { useEffect, useState } from "react";
import { useParams, Link } from "react-router-dom";
import axios from "axios";
import { Copy, Check, ArrowRight } from "lucide-react";
import { toast, Toaster } from "sonner";
import StatusBadge from "../components/StatusBadge";
import { qualityColor, qualityLabel } from "../lib/format";
import { API } from "../lib/api";

function setMetaTag(attr, attrValue, content) {
  if (typeof document === "undefined") return;
  let el = document.querySelector(`meta[${attr}="${attrValue}"]`);
  if (!el) {
    el = document.createElement("meta");
    el.setAttribute(attr, attrValue);
    document.head.appendChild(el);
  }
  el.setAttribute("content", content || "");
}

function setCanonical(href) {
  if (typeof document === "undefined") return;
  let el = document.querySelector('link[rel="canonical"]');
  if (!el) {
    el = document.createElement("link");
    el.setAttribute("rel", "canonical");
    document.head.appendChild(el);
  }
  el.setAttribute("href", href);
}

function useShareSocialMeta(data, url) {
  useEffect(() => {
    if (!data) return;
    const title = data.display_title || data.project_name || "FacelessForge share";
    const desc = data.metadata?.description
      ? (data.metadata.description.slice(0, 240) + (data.metadata.description.length > 240 ? "…" : ""))
      : `Created with FacelessForge · quality ${data.quality_score}/100 · ${data.niche}`;
    const og = {
      title: `${title} · FacelessForge`,
      description: desc,
      url,
      image: `${window.location.origin}/og-share-default.svg`,
      type: "article",
      site: "FacelessForge",
    };

    const prevTitle = document.title;
    document.title = og.title;
    setMetaTag("name", "description", desc);
    setMetaTag("property", "og:title", og.title);
    setMetaTag("property", "og:description", og.description);
    setMetaTag("property", "og:url", og.url);
    setMetaTag("property", "og:image", og.image);
    setMetaTag("property", "og:type", og.type);
    setMetaTag("property", "og:site_name", og.site);
    setMetaTag("name", "twitter:card", "summary_large_image");
    setMetaTag("name", "twitter:title", og.title);
    setMetaTag("name", "twitter:description", og.description);
    setMetaTag("name", "twitter:image", og.image);
    setCanonical(url);

    return () => {
      document.title = prevTitle;
    };
  }, [data, url]);
}

function CopyChip({ text, label, testId }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      toast.success("Copied");
      setTimeout(() => setCopied(false), 1500);
    } catch { toast.error("Copy failed"); }
  };
  return (
    <button
      onClick={copy}
      data-testid={testId}
      className="inline-flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-widest text-zinc-400 hover:text-[#00E5FF] border border-zinc-800 hover:border-[#00E5FF] px-2 py-1 rounded-sm transition-colors"
    >
      {copied ? <Check size={12} strokeWidth={1.8} /> : <Copy size={12} strokeWidth={1.5} />}
      {copied ? "Copied" : label}
    </button>
  );
}

export default function PublicSharePage() {
  const { token } = useParams();
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const publicUrl = typeof window !== "undefined" ? `${window.location.origin}/s/${token}` : `/s/${token}`;

  useEffect(() => {
    (async () => {
      try {
        const { data } = await axios.get(`${API}/public/share/${token}`);
        setData(data);
      } catch (err) {
        setError(err.response?.status === 404 ? "This share link is no longer active." : "Could not load share.");
      }
    })();
  }, [token]);

  useShareSocialMeta(data, publicUrl);

  if (error) {
    return (
      <div className="min-h-screen bg-[#0A0A0A] text-white flex items-center justify-center px-6">
        <div className="text-center space-y-4">
          <div className="font-mono text-[10px] uppercase tracking-widest text-[#FF3366]">404 · Not found</div>
          <h1 className="text-3xl font-bold tracking-tight">{error}</h1>
          <Link to="/" className="inline-flex items-center gap-2 text-[#00E5FF] hover:text-[#33EFFF]">
            Go to FacelessForge <ArrowRight size={14} />
          </Link>
        </div>
      </div>
    );
  }

  if (!data) {
    return (
      <div className="min-h-screen bg-[#0A0A0A] text-zinc-500 font-mono text-sm flex items-center justify-center">
        Loading…
      </div>
    );
  }

  const { display_title, project_name, niche, status, quality_score, metadata, thumbnails } = data;
  const color = qualityColor(quality_score);

  return (
    <div className="min-h-screen bg-[#0A0A0A] text-white">
      <Toaster
        theme="dark"
        position="top-right"
        toastOptions={{
          style: { background: "#121212", border: "1px solid #27272A", color: "#fff", borderRadius: 2, fontFamily: "'JetBrains Mono', monospace", fontSize: 12 },
        }}
      />

      <header className="ff-grid border-b border-zinc-800">
        <div className="max-w-5xl mx-auto px-6 py-8 md:py-14">
          <div className="flex items-center gap-3 mb-8">
            <StatusBadge status={status} />
            <span className="font-mono text-[10px] uppercase tracking-widest text-zinc-500">{niche}</span>
          </div>

          <h1 data-testid="share-display-title" className="text-3xl md:text-5xl font-bold tracking-tight leading-[1.1] max-w-4xl">
            {display_title}
          </h1>
          {display_title !== project_name && (
            <div className="mt-4 font-mono text-[10px] uppercase tracking-widest text-zinc-600">
              Internal name · {project_name}
            </div>
          )}

          <div className="mt-8 flex items-center gap-6">
            <div>
              <div className="font-mono text-[10px] uppercase tracking-widest text-zinc-500 mb-1">Quality score</div>
              <div className="flex items-center gap-3">
                <span className="font-mono text-3xl font-semibold tabular-nums" style={{ color }}>
                  {quality_score}<span className="text-zinc-600 text-lg">/100</span>
                </span>
                <span className="font-mono text-[10px] uppercase tracking-widest" style={{ color }}>
                  {qualityLabel(quality_score)}
                </span>
              </div>
            </div>
          </div>
        </div>
      </header>

      <main className="max-w-5xl mx-auto px-6 py-10 space-y-6">
        {!metadata ? (
          <div className="border border-zinc-800 border-dashed p-10 text-center rounded-sm text-zinc-400">
            Metadata is not ready yet.
          </div>
        ) : (
          <>
            {/* Selected title */}
            <div className="border border-zinc-800 bg-[#121212] rounded-sm">
              <div className="px-5 py-3 border-b border-zinc-800 flex items-center justify-between">
                <span className="font-mono text-[10px] uppercase tracking-widest text-zinc-500">YouTube title</span>
                <CopyChip text={metadata.selected_title} label="Copy" testId="share-copy-title" />
              </div>
              <div className="p-5 text-lg text-white font-medium leading-relaxed">
                {metadata.selected_title}
              </div>
            </div>

            {/* Description */}
            {metadata.description && (
              <div className="border border-zinc-800 bg-[#121212] rounded-sm">
                <div className="px-5 py-3 border-b border-zinc-800 flex items-center justify-between">
                  <span className="font-mono text-[10px] uppercase tracking-widest text-zinc-500">Description</span>
                  <CopyChip text={metadata.description} label="Copy" testId="share-copy-description" />
                </div>
                <pre className="p-5 text-sm text-zinc-200 leading-relaxed whitespace-pre-wrap font-sans">
                  {metadata.description}
                </pre>
              </div>
            )}

            {/* Tags + Hashtags + Chapters */}
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
              {metadata.tags?.length > 0 && (
                <div className="border border-zinc-800 bg-[#121212] rounded-sm">
                  <div className="px-5 py-3 border-b border-zinc-800 flex items-center justify-between">
                    <span className="font-mono text-[10px] uppercase tracking-widest text-zinc-500">Tags</span>
                    <CopyChip text={metadata.tags.join(", ")} label="Copy all" testId="share-copy-tags" />
                  </div>
                  <div className="p-5 flex flex-wrap gap-2">
                    {metadata.tags.map((t, i) => (
                      <span key={i} className="font-mono text-xs px-2 py-1 border border-zinc-800 text-zinc-300 rounded-sm">{t}</span>
                    ))}
                  </div>
                </div>
              )}
              {(metadata.hashtags?.length > 0 || metadata.chapters?.length > 0) && (
                <div className="border border-zinc-800 bg-[#121212] rounded-sm">
                  {metadata.hashtags?.length > 0 && (
                    <>
                      <div className="px-5 py-3 border-b border-zinc-800 flex items-center justify-between">
                        <span className="font-mono text-[10px] uppercase tracking-widest text-zinc-500">Hashtags</span>
                      </div>
                      <div className="p-5 flex flex-wrap gap-2">
                        {metadata.hashtags.map((h, i) => (
                          <span key={i} className="font-mono text-xs px-2 py-1 border border-zinc-800 text-[#7B61FF] rounded-sm">{h}</span>
                        ))}
                      </div>
                    </>
                  )}
                  {metadata.chapters?.length > 0 && (
                    <>
                      <div className="px-5 py-3 border-t border-b border-zinc-800">
                        <span className="font-mono text-[10px] uppercase tracking-widest text-zinc-500">Chapters</span>
                      </div>
                      <ul className="p-5 space-y-1.5">
                        {metadata.chapters.map((c, i) => (
                          <li key={i} className="flex items-center gap-3 text-sm">
                            <span className="font-mono text-[11px] text-[#00E5FF] w-14">{c.timestamp}</span>
                            <span className="text-zinc-300">{c.title}</span>
                          </li>
                        ))}
                      </ul>
                    </>
                  )}
                </div>
              )}
            </div>

            {/* Pinned comment */}
            {metadata.pinned_comment && (
              <div className="border border-zinc-800 bg-[#121212] rounded-sm">
                <div className="px-5 py-3 border-b border-zinc-800 flex items-center justify-between">
                  <span className="font-mono text-[10px] uppercase tracking-widest text-zinc-500">Pinned comment</span>
                  <CopyChip text={metadata.pinned_comment} label="Copy" testId="share-copy-pinned" />
                </div>
                <div className="p-5 text-sm text-zinc-200 leading-relaxed italic">
                  "{metadata.pinned_comment}"
                </div>
              </div>
            )}

            {/* Thumbnail briefs */}
            {thumbnails?.length > 0 && (
              <section>
                <h2 className="text-sm font-semibold mb-3">Thumbnail concepts</h2>
                <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                  {thumbnails.map((t, i) => (
                    <div key={i} className="border border-zinc-800 bg-[#121212] rounded-sm overflow-hidden">
                      <div className="px-5 py-4 border-b border-zinc-800 bg-[#0A0A0A]">
                        <div className="font-mono text-[10px] uppercase tracking-widest text-zinc-500 mb-2">
                          Concept · {String(i + 1).padStart(2, "0")}
                        </div>
                        <div className="text-xl font-bold tracking-tight">{t.brief.thumbnail_title_text}</div>
                      </div>
                      <div className="p-5 space-y-2 text-sm">
                        <Row label="Composition" value={t.brief.visual_composition} />
                        <Row label="Emotion" value={t.brief.emotion_angle} />
                        <Row label="Colour" value={t.brief.colour_direction} />
                        <Row label="Click trigger" value={t.brief.click_trigger} />
                      </div>
                    </div>
                  ))}
                </div>
              </section>
            )}
          </>
        )}
      </main>

      <footer className="mt-12 border-t border-zinc-800 bg-[#0A0A0A]">
        <div className="max-w-5xl mx-auto px-6 py-8 flex flex-col md:flex-row items-start md:items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            <div
              className="w-8 h-8 flex items-center justify-center bg-[#00E5FF] text-black font-black font-mono text-lg"
              style={{ clipPath: "polygon(0 0, 100% 0, 100% 70%, 85% 100%, 0 100%)" }}
            >
              F
            </div>
            <div className="leading-tight">
              <div className="font-semibold tracking-tight">Created with FacelessForge</div>
              <div className="font-mono text-[10px] text-zinc-500 tracking-widest uppercase">
                Turn any idea into a YouTube-ready content package.
              </div>
            </div>
          </div>
          <Link
            to="/register"
            data-testid="share-cta-register"
            className="inline-flex items-center gap-2 bg-[#00E5FF] text-black font-semibold text-sm px-4 py-2 rounded-sm hover:bg-[#33EFFF] transition-colors"
          >
            Build your own <ArrowRight size={14} strokeWidth={2} />
          </Link>
        </div>
      </footer>
    </div>
  );
}

function Row({ label, value }) {
  return (
    <div className="grid grid-cols-[100px_1fr] gap-3">
      <span className="font-mono text-[10px] uppercase tracking-widest text-zinc-500 pt-0.5">{label}</span>
      <span className="text-zinc-200 leading-relaxed">{value}</span>
    </div>
  );
}
