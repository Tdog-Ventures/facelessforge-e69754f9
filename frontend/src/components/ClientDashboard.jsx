import React, { useState, useEffect, useCallback } from "react";
import LengthFader from "./LengthFader";

const API_BASE = "/api";

function IntakeForm({ onCreated }) {
  const [form, setForm] = useState({
    name: "",
    niche: "",
    topic: "",
    audience: "",
    tone: "",
    target_duration: 120,
  });
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState(null);

  const handleChange = (field) => (e) =>
    setForm((f) => ({ ...f, [field]: e.target.value }));

  const handleSubmit = async (e) => {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE}/intake`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...form,
          target_duration: Number(form.target_duration),
        }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `Request failed (${res.status})`);
      }
      const data = await res.json();
      onCreated?.(data);
      setForm({
        name: "",
        niche: "",
        topic: "",
        audience: "",
        tone: "",
        target_duration: 120,
      });
    } catch (err) {
      setError(err.message);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <form onSubmit={handleSubmit} className="space-y-4 max-w-lg">
      <h2 className="text-xl font-semibold">Request a video</h2>
      <input className="w-full border rounded px-3 py-2" placeholder="Video name" value={form.name} onChange={handleChange("name")} required />
      <input className="w-full border rounded px-3 py-2" placeholder="Niche (e.g. HVAC tips, business case study)" value={form.niche} onChange={handleChange("niche")} required />
      <textarea className="w-full border rounded px-3 py-2" placeholder="Topic / brief (min 10 characters)" value={form.topic} onChange={handleChange("topic")} rows={4} required />
      <input className="w-full border rounded px-3 py-2" placeholder="Audience (e.g. Adelaide homeowners)" value={form.audience} onChange={handleChange("audience")} required />
      <input className="w-full border rounded px-3 py-2" placeholder="Tone (e.g. friendly, professional)" value={form.tone} onChange={handleChange("tone")} required />
      <div className="block text-sm">
        <label htmlFor="client-length" className="block">Length</label>
        <span className="block text-xs text-[var(--ff-text-dim)] mb-2">5 seconds – 60 minutes</span>
        <LengthFader
          id="client-length"
          testId="client-length"
          value={form.target_duration}
          onChange={(seconds) => setForm((current) => ({ ...current, target_duration: seconds }))}
        />
      </div>
      {error && <p className="text-[var(--ff-error)] text-sm">{error}</p>}
      <button type="submit" disabled={submitting} className="bg-[var(--ff-primary)] text-white px-4 py-2 rounded hover:bg-[var(--ff-primary-hover)] disabled:opacity-50">
        {submitting ? "Submitting..." : "Generate video"}
      </button>
    </form>
  );
}

function StatusBadge({ status }) {
  const colors = {
    queued: "bg-amber-500/15 text-[var(--ff-warning)]",
    rendering: "bg-blue-500/15 text-[var(--ff-primary)]",
    completed: "bg-emerald-500/15 text-[var(--ff-success)]",
    COMPLETED: "bg-emerald-500/15 text-[var(--ff-success)]",
    failed: "bg-red-500/15 text-[var(--ff-error)]",
    FAILED: "bg-red-500/15 text-[var(--ff-error)]",
  };
  const cls = colors[status] || "bg-[var(--ff-surface-2)] text-[var(--ff-text-muted)]";
  return <span className={`text-xs px-2 py-1 rounded ${cls}`}>{status || "unknown"}</span>;
}

function VideoList() {
  const [videos, setVideos] = useState([]);
  const [loading, setLoading] = useState(true);

  const fetchVideos = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/my-videos`, { credentials: "include" });
      if (res.ok) setVideos(await res.json());
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchVideos();
    const interval = setInterval(fetchVideos, 10000);
    return () => clearInterval(interval);
  }, [fetchVideos]);

  if (loading) return <p>Loading...</p>;
  if (videos.length === 0) return <p className="text-[var(--ff-text-dim)]">No videos yet.</p>;

  return (
    <div className="space-y-3">
      {videos.map((v) => (
        <div key={v.project_id} className="border rounded p-4 flex justify-between items-center">
          <div>
            <p className="font-medium">{v.name}</p>
            <p className="text-sm text-[var(--ff-text-dim)]">{v.topic}</p>
            {v.error_message && <p className="text-sm text-[var(--ff-error)]">{v.error_message}</p>}
          </div>
          <div className="flex items-center gap-3">
            <StatusBadge status={v.status} />
            {v.output_url && (
              <a href={v.output_url} target="_blank" rel="noreferrer" className="text-[var(--ff-primary)] text-sm underline">View</a>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}

export default function ClientDashboard() {
  const [refreshKey, setRefreshKey] = useState(0);
  return (
    <div className="p-6 space-y-8">
      <IntakeForm onCreated={() => setRefreshKey((k) => k + 1)} />
      <div key={refreshKey}>
        <h2 className="text-xl font-semibold mb-3">Your videos</h2>
        <VideoList />
      </div>
    </div>
  );
}
