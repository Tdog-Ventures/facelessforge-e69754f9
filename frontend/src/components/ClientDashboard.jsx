import React, { useState, useEffect, useCallback } from "react";

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
      <label className="block text-sm">
        Target duration (seconds)
        <input type="number" className="w-full border rounded px-3 py-2 mt-1" value={form.target_duration} onChange={handleChange("target_duration")} min={30} max={3600} />
      </label>
      {error && <p className="text-red-600 text-sm">{error}</p>}
      <button type="submit" disabled={submitting} className="bg-black text-white px-4 py-2 rounded disabled:opacity-50">
        {submitting ? "Submitting..." : "Generate video"}
      </button>
    </form>
  );
}

function StatusBadge({ status }) {
  const colors = {
    queued: "bg-yellow-100 text-yellow-800",
    rendering: "bg-blue-100 text-blue-800",
    completed: "bg-green-100 text-green-800",
    COMPLETED: "bg-green-100 text-green-800",
    failed: "bg-red-100 text-red-800",
    FAILED: "bg-red-100 text-red-800",
  };
  const cls = colors[status] || "bg-gray-100 text-gray-800";
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
  if (videos.length === 0) return <p className="text-gray-500">No videos yet.</p>;

  return (
    <div className="space-y-3">
      {videos.map((v) => (
        <div key={v.project_id} className="border rounded p-4 flex justify-between items-center">
          <div>
            <p className="font-medium">{v.name}</p>
            <p className="text-sm text-gray-500">{v.topic}</p>
            {v.error_message && <p className="text-sm text-red-600">{v.error_message}</p>}
          </div>
          <div className="flex items-center gap-3">
            <StatusBadge status={v.status} />
            {v.output_url && (
              <a href={v.output_url} target="_blank" rel="noreferrer" className="text-blue-600 text-sm underline">View</a>
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
