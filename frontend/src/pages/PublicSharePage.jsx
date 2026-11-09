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
    const brand = data.branding?.white_label_enabled && data.branding?.brand_name ? data.branding.brand_name : "FacelessForge";
    const title = data.display_title || data.project_name || `${brand} share`;
    const desc = data.metadata?.description
      ? (data.metadata.description.slice(0, 240) + (data.metadata.description.length > 240 ? "…" : ""))
      : `Created with ${brand} · quality ${data.quality_score}/100 · ${data.niche}`;
    const image = data.selected_thumbnail_url || `${window.location.origin}/og-share-default.svg`;
    const og = {
      title: `${title} · ${brand}`,
      description: desc,
      url,
      image,
      type: "article",
      site: brand,
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
      className="inline-flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-widest text-[var(--ff-text-muted)] hover:text-[#00E5FF] border border-[var(--ff-border)] hover:border-[#00E5FF] px-2 py-1 rounded-sm transition-colors"
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
      <div className="min-h-screen bg-[var(--ff-input-bg)] text-[var(--ff-text)] flex items-center justify-center px-6">
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
      <div className="min-h-screen bg-[var(--ff-input-bg)] text-[var(--ff-text-muted)] font-mono text-sm flex items-center justify-center">
        Loading…
      </div>
    );
  }

  const { display_title, project_name, niche, status, quality_score, metadata, thumbnails, selected_thumbnail_url, selected_voiceover, final_video } = data;
  const color = qualityColor(quality_score);

  return (
    <div className="min-h-screen bg-[var(--ff-input-bg)] text-[var(--ff-text)]">
      <Toaster
        theme="system"
        position="top-right"
        toastOptions={{
          style: { background: "var(--ff-surface)", border: "1px solid var(--ff-border)", color: "var(--ff-text)", borderRadius: 2, fontFamily: "'JetBrains Mono', monospace", fontSize: 12 },
        }}
      />

      <header className="ff-grid border-b border-[var(--ff-border)]">
        <div className="max-w-5xl mx-auto px-6 py-8 md:py-14">
          <div className="flex items-center gap-3 mb-8">
            <StatusBadge status={status} />
            <span className="font-mono text-[10px] uppercase tracking-widest text-[var(--ff-text-muted)]">{niche}</span>
          </div>

          {selected_thumbnail_url && (
            <div className="mb-10 border border-[var(--ff-border)] rounded-sm overflow-hidden bg-[var(--ff-surface)]" data-testid="share-hero-thumbnail">
              {final_video?.url ? (
                <video
                  data-testid="share-final-video"
                  controls
                  poster={selected_thumbnail_url}
                  src={final_video.url}
                  preload="metadata"
                  className="w-full block"
                  style={{ aspectRatio: "16 / 9", objectFit: "cover" }}
                />
              ) : (
                <img
                  src={selected_thumbnail_url}
                  alt={display_title}
                  className="w-full h-auto block"
                  style={{ aspectRatio: "16 / 9", objectFit: "cover" }}
                  loading="eager"
                />
              )}
            </div>
          )}

          <h1 data-testid="share-display-title" className="text-3xl md:text-5xl font-bold tracking-tight leading-[1.1] max-w-4xl">
            {display_title}
          </h1>
          {display_title !== project_name && (
            <div className="mt-4 font-mono text-[10px] uppercase tracking-widest text-[var(--ff-text-dim)]">
              Internal name · {project_name}
            </div>
          )}

          <div className="mt-8 flex items-center gap-6">
            <div>
              <div className="font-mono text-[10px] uppercase tracking-widest text-[var(--ff-text-muted)] mb-1">Quality score</div>
              <div className="flex items-center gap-3">
                <span className="font-mono text-3xl font-semibold tabular-nums" style={{ color }}>
                  {quality_score}<span className="text-[var(--ff-text-dim)] text-lg">/100</span>
                </span>
                <span className="font-mono text-[10px] uppercase tracking-widest" style={{ color }}>
                  {qualityLabel(quality_score)}
                </span>
              </div>
            </div>
          </div>

          {selected_voiceover?.preview_url && (
            <div
              data-testid="share-voiceover-player"
              className="mt-8 border border-[var(--ff-border)] bg-[var(--ff-surface)] rounded-sm p-4 space-y-2 max-w-2xl"
            >
              <div className="flex items-center gap-2">
                <span className="font-mono text-[10px] uppercase tracking-widest text-[#FF66CC]">
                  Preview voiceover
                </span>
                {selected_voiceover.voice_style && (
                  <span className="font-mono text-[10px] uppercase tracking-widest text-[var(--ff-text-muted)]">
                    · {selected_voiceover.voice_style}
                  </span>
                )}
              </div>
              <audio controls preload="metadata" src={selected_voiceover.preview_url} className="w-full h-10" />
            </div>
          )}
        </div>
      </header>

      <main className="max-w-5xl mx-auto px-6 py-10 space-y-6">
        {!metadata ? (
          <div className="border border-[var(--ff-border)] border-dashed p-10 text-center rounded-sm text-[var(--ff-text-muted)]">
            Metadata is not ready yet.
          </div>
        ) : (
          <>
            {/* Selected title */}
            <div className="border border-[var(--ff-border)] bg-[var(--ff-surface)] rounded-sm">
              <div className="px-5 py-3 border-b border-[var(--ff-border)] flex items-center justify-between">
                <span className="font-mono text-[10px] uppercase tracking-widest text-[var(--ff-text-muted)]">YouTube title</span>
                <CopyChip text={metadata.selected_title} label="Copy" testId="share-copy-title" />
              </div>
              <div className="p-5 text-lg text-[var(--ff-text)] font-medium leading-relaxed">
                {metadata.selected_title}
              </div>
            </div>

            {/* Description */}
            {metadata.description && (
              <div className="border border-[var(--ff-border)] bg-[var(--ff-surface)] rounded-sm">
                <div className="px-5 py-3 border-b border-[var(--ff-border)] flex items-center justify-between">
                  <span className="font-mono text-[10px] uppercase tracking-widest text-[var(--ff-text-muted)]">Description</span>
                  <CopyChip text={metadata.description} label="Copy" testId="share-copy-description" />
                </div>
                <pre className="p-5 text-sm text-[var(--ff-text)] leading-relaxed whitespace-pre-wrap font-sans">
                  {metadata.description}
                </pre>
              </div>
            )}

            {/* Tags + Hashtags + Chapters */}
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
              {metadata.tags?.length > 0 && (
                <div className="border border-[var(--ff-border)] bg-[var(--ff-surface)] rounded-sm">
                  <div className="px-5 py-3 border-b border-[var(--ff-border)] flex items-center justify-between">
                    <span className="font-mono text-[10px] uppercase tracking-widest text-[var(--ff-text-muted)]">Tags</span>
                    <CopyChip text={metadata.tags.join(", ")} label="Copy all" testId="share-copy-tags" />
                  </div>
                  <div className="p-5 flex flex-wrap gap-2">
                    {metadata.tags.map((t) => (
                      <span key={`tag-${t}`} className="font-mono text-xs px-2 py-1 border border-[var(--ff-border)] text-[var(--ff-text)] rounded-sm">{t}</span>
                    ))}
                  </div>
                </div>
              )}
              {(metadata.hashtags?.length > 0 || metadata.chapters?.length > 0) && (
                <div className="border border-[var(--ff-border)] bg-[var(--ff-surface)] rounded-sm">
                  {metadata.hashtags?.length > 0 && (
                    <>
                      <div className="px-5 py-3 border-b border-[var(--ff-border)] flex items-center justify-between">
                        <span className="font-mono text-[10px] uppercase tracking-widest text-[var(--ff-text-muted)]">Hashtags</span>
                      </div>
                      <div className="p-5 flex flex-wrap gap-2">
                        {metadata.hashtags.map((h) => (
                          <span key={`hashtag-${h}`} className="font-mono text-xs px-2 py-1 border border-[var(--ff-border)] text-[#7B61FF] rounded-sm">{h}</span>
                        ))}
                      </div>
                    </>
                  )}
                  {metadata.chapters?.length > 0 && (
                    <>
                      <div className="px-5 py-3 border-t border-b border-[var(--ff-border)]">
                        <span className="font-mono text-[10px] uppercase tracking-widest text-[var(--ff-text-muted)]">Chapters</span>
                      </div>
                      <ul className="p-5 space-y-1.5">
                        {metadata.chapters.map((c) => (
                          <li key={`chapter-${c.timestamp}-${c.title}`} className="flex items-center gap-3 text-sm">
                            <span className="font-mono text-[11px] text-[#00E5FF] w-14">{c.timestamp}</span>
                            <span className="text-[var(--ff-text)]">{c.title}</span>
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
              <div className="border border-[var(--ff-border)] bg-[var(--ff-surface)] rounded-sm">
                <div className="px-5 py-3 border-b border-[var(--ff-border)] flex items-center justify-between">
                  <span className="font-mono text-[10px] uppercase tracking-widest text-[var(--ff-text-muted)]">Pinned comment</span>
                  <CopyChip text={metadata.pinned_comment} label="Copy" testId="share-copy-pinned" />
                </div>
                <div className="p-5 text-sm text-[var(--ff-text)] leading-relaxed italic">
                  "{metadata.pinned_comment}"
                </div>
              </div>
            )}

            {/* MP4 Download + GCS Mirror */}
            {data.final_video?.url && (
              <div className="border border-[#00FF66]/20 bg-[#00FF66]/5 rounded-sm p-5 space-y-3" data-testid="share-download-panel">
                <div className="font-mono text-[10px] uppercase tracking-widest text-[#00FF66]">MP4 Download — Ready</div>
                <div className="flex flex-wrap gap-2">
                  <a
                    data-testid="share-download-mp4"
                    href={data.final_video.url}
                    download={`${(display_title || "facelessforge").replace(/[^a-z0-9_-]/gi, "_")}.mp4`}
                    className="inline-flex items-center gap-2 bg-[#00FF66] text-black font-semibold text-sm px-4 py-2 rounded-sm hover:bg-[#33FF80]"
                  >
                    ⬇ Download MP4
                  </a>
                  <a
                    data-testid="share-gcs-mirror"
                    href={data.download?.gcs_mirror || data.final_video.url}
                    target="_blank"
                    rel="noreferrer"
                    className="inline-flex items-center gap-2 border border-[var(--ff-input-border)] text-[var(--ff-text)] hover:border-[#00E5FF] hover:text-[#00E5FF] font-mono text-[11px] uppercase tracking-widest px-4 py-2 rounded-sm"
                  >
                    GCS Mirror (R2) — videos.ethinx.solutions
                  </a>
                </div>
                <div className="font-mono text-[11px] text-[var(--ff-text-muted)] break-all">
                  {data.final_video.url}
                </div>
                <div className="font-mono text-[10px] text-[var(--ff-text-dim)]">
                  R2 bucket facelessforge-prod · GCS mirror compatible · 1080p H.264/AAC
                </div>
              </div>
            )}

            {/* Posting Guidance */}
            <div className="border border-[#00E5FF]/20 bg-[#00E5FF]/5 rounded-sm p-5 space-y-3" data-testid="share-posting-guidance">
              <div className="font-mono text-[10px] uppercase tracking-widest text-[#00E5FF]">Posting Guidance — YouTube / TikTok / Reels</div>
              <ol className="list-decimal list-inside space-y-1.5 text-sm text-[var(--ff-text)]">
                <li>Upload MP4 native — don't re-encode (1080p 30fps H.264/AAC)</li>
                <li>Title: Copy YouTube title above (&lt;70 chars for CTR)</li>
                <li>Description: Paste full description + add your company link first line</li>
                <li>Tags: Copy tags block; add niche hashtag + company name</li>
                <li>Thumbnail: Download selected thumbnail as custom thumbnail 1280×720</li>
                <li>Scheduling: Post 10am-2pm local, add chapters, pin comment</li>
                <li>Shorts/Reels: Crop center 1080×1920 if vertical needed — audio already mixed</li>
              </ol>
              <p className="font-mono text-[11px] text-[var(--ff-text-muted)]">
                Questions? Reply — support@ethinx.solutions · EthinX Solutions ABN 60 578 933 517
              </p>
            </div>

            {/* Thumbnail briefs */}
            {thumbnails?.length > 0 && (
              <section>
                <h2 className="text-sm font-semibold mb-3">Thumbnail concepts</h2>
                <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                  {thumbnails.map((t, i) => (
                    <div key={t.id || `thumb-${i}`} className="border border-[var(--ff-border)] bg-[var(--ff-surface)] rounded-sm overflow-hidden">
                      <div className="px-5 py-4 border-b border-[var(--ff-border)] bg-[var(--ff-input-bg)]">
                        <div className="font-mono text-[10px] uppercase tracking-widest text-[var(--ff-text-muted)] mb-2">
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

      <footer className="mt-12 border-t border-[var(--ff-border)] bg-[var(--ff-input-bg)]">
        <div className="max-w-5xl mx-auto px-6 py-8 flex flex-col md:flex-row items-start md:items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            <div
              className="w-8 h-8 flex items-center justify-center bg-[#00E5FF] text-black font-black font-mono text-lg"
              style={{ clipPath: "polygon(0 0, 100% 0, 100% 70%, 85% 100%, 0 100%)", background: data.branding?.brand_color || "#00E5FF" }}
            >
              {(data.branding?.white_label_enabled && data.branding?.brand_name ? data.branding.brand_name[0] : "F").toUpperCase()}
            </div>
            <div className="leading-tight">
              <div className="font-semibold tracking-tight">Created with {data.branding?.white_label_enabled && data.branding?.brand_name ? data.branding.brand_name : "FacelessForge"}</div>
              <div className="font-mono text-[10px] text-[var(--ff-text-muted)] tracking-widest uppercase">
                {data.branding?.white_label_enabled ? `White-label via tenant settings · custom domain ${data.branding?.custom_domain || "—"}` : "Turn any idea into a YouTube-ready content package."}
              </div>
              {data.branding?.brand_logo_url && data.branding?.white_label_enabled && (
                <img src={data.branding.brand_logo_url} alt="brand logo" className="h-6 mt-2" />
              )}
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
      <span className="font-mono text-[10px] uppercase tracking-widest text-[var(--ff-text-muted)] pt-0.5">{label}</span>
      <span className="text-[var(--ff-text)] leading-relaxed">{value}</span>
    </div>
  );
}
