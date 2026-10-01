import React, { useEffect, useState } from "react";
import { toast } from "sonner";
import {
  RefreshCw, Loader2, CheckCircle2, AlertCircle, Database, HardDrive, Shield, Zap,
} from "lucide-react";
import AppShell from "../components/AppShell";
import TopBar from "../components/TopBar";
import { api, formatApiError } from "../lib/api";
import { useConfirm } from "../components/ConfirmDialog";

function fmtBytes(n) {
  if (!n && n !== 0) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

function StatusDot({ ok }) {
  return ok
    ? <CheckCircle2 size={16} className="text-[#00FF66]" />
    : <AlertCircle size={16} className="text-[#FF3366]" />;
}

function Row({ label, value, ok }) {
  return (
    <div className="flex items-start gap-3 py-2 border-b border-zinc-800 last:border-b-0">
      <div className="font-mono text-[10px] uppercase tracking-widest text-zinc-500 w-44 pt-0.5 shrink-0">
        {label}
      </div>
      <div className="flex-1 text-sm text-zinc-200 break-all">{value}</div>
      {ok !== undefined && <StatusDot ok={ok} />}
    </div>
  );
}

export default function AdminDiagnosticsPage() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const confirm = useConfirm();

  const fetchOne = async () => {
    try {
      const { data } = await api.get("/admin/diagnostics");
      setData(data);
    } catch (err) {
      toast.error("Failed to load diagnostics", {
        description: formatApiError(err.response?.data?.detail) || err.message,
      });
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { fetchOne(); }, []);

  const runRetention = async () => {
    const ok = await confirm({
      title: "Run retention sweep now?",
      description: "Removes render MP4s older than the retention window, stale work dirs, and orphan project files. Safe — only deletes generated artifacts.",
      confirmLabel: "Run cleanup",
    });
    if (!ok) return;
    setRunning(true);
    try {
      const { data: report } = await api.post("/admin/retention/run");
      toast.success("Retention sweep complete", {
        description: `Freed ${fmtBytes(report.bytes_freed)} · ${report.renders_removed} renders, ${report.render_workdirs_removed} work dirs, ${report.orphan_project_dirs_removed} orphan dirs`,
      });
      await fetchOne();
    } catch (err) {
      toast.error("Retention failed", {
        description: formatApiError(err.response?.data?.detail) || err.message,
      });
    } finally {
      setRunning(false);
    }
  };

  if (loading) {
    return (
      <AppShell>
        <TopBar title="Diagnostics" />
        <div className="p-8 text-sm text-zinc-500 font-mono">Loading…</div>
      </AppShell>
    );
  }
  if (!data) {
    return (
      <AppShell>
        <TopBar title="Diagnostics" />
        <div className="p-8 text-sm text-[#FF3366]">Failed to load.</div>
      </AppShell>
    );
  }

  const allOk = data.ok
    && !data.cors.wildcard
    && !!data.binaries.ffmpeg_path
    && (data.dev_mode || data.cookie_mode === "none+secure")
    && data.storage.probe_ok === true;

  return (
    <AppShell>
      <TopBar
        title="Diagnostics"
        subtitle="Production readiness · admin only"
        right={
          <div className="flex items-center gap-2">
            <button
              data-testid="diag-refresh"
              onClick={fetchOne}
              className="flex items-center gap-1 font-mono text-[10px] uppercase tracking-widest text-zinc-400 hover:text-[#00E5FF] border border-zinc-800 hover:border-[#00E5FF] px-3 py-1.5 rounded-sm transition-colors"
            >
              <RefreshCw size={11} /> Refresh
            </button>
            <button
              data-testid="diag-run-retention"
              onClick={runRetention}
              disabled={running}
              className="flex items-center gap-1 font-mono text-[10px] uppercase tracking-widest text-zinc-300 hover:text-white border border-[#FFB020]/40 hover:border-[#FFB020] bg-[#FFB020]/10 px-3 py-1.5 rounded-sm transition-colors disabled:opacity-50"
            >
              {running ? <Loader2 size={11} className="animate-spin" /> : <HardDrive size={11} />}
              Run retention sweep
            </button>
          </div>
        }
      />

      <div className="p-8 space-y-6">
        {/* Top banner */}
        <div
          data-testid="diag-banner"
          className={`border p-5 rounded-sm flex items-start gap-3 ${
            allOk ? "border-[#00FF66]/30 bg-[#00FF66]/5"
                 : "border-[#FFB020]/30 bg-[#FFB020]/5"
          }`}
        >
          {allOk
            ? <CheckCircle2 size={22} className="text-[#00FF66] mt-0.5" />
            : <AlertCircle size={22} className="text-[#FFB020] mt-0.5" />}
          <div>
            <div className="font-mono text-[11px] uppercase tracking-widest"
                 style={{ color: allOk ? "#00FF66" : "#FFB020" }}>
              {allOk ? "All checks pass" : "Review required"}
            </div>
            <div className="text-sm text-zinc-300 mt-1">
              {allOk
                ? "ffmpeg available, CORS locked to FRONTEND_URL, cookies in production mode."
                : data.dev_mode
                ? "DEV_MODE is on — switch to production by setting DEV_MODE=false and providing FRONTEND_URL."
                : "Inspect failing rows below before public deploy."}
            </div>
          </div>
        </div>

        {/* Binaries */}
        <section className="border border-zinc-800 bg-[#121212] rounded-sm p-5" data-testid="diag-binaries">
          <h2 className="text-sm font-semibold text-white mb-3 flex items-center gap-2">
            <Zap size={14} className="text-[#00E5FF]" /> Binaries
          </h2>
          <Row label="ffmpeg path" value={data.binaries.ffmpeg_path || "missing"}
               ok={!!data.binaries.ffmpeg_path} />
          <Row label="ffmpeg source" value={data.binaries.ffmpeg_source || "—"} />
          <Row label="ffprobe path" value={data.binaries.ffprobe_path || "missing (estimated duration)"}
               ok={!!data.binaries.ffprobe_path} />
          <Row label="ffprobe source" value={data.binaries.ffprobe_source || "—"} />
        </section>

        {/* Security */}
        <section className="border border-zinc-800 bg-[#121212] rounded-sm p-5" data-testid="diag-security">
          <h2 className="text-sm font-semibold text-white mb-3 flex items-center gap-2">
            <Shield size={14} className="text-[#7B61FF]" /> Security
          </h2>
          <Row label="DEV_MODE" value={data.dev_mode ? "true" : "false"} ok={!data.dev_mode} />
          <Row label="Cookie mode" value={data.cookie_mode}
               ok={data.dev_mode || data.cookie_mode === "none+secure"} />
          <Row label="CORS origins" value={(data.cors.origins || []).join(", ") || "(none)"}
               ok={!data.cors.wildcard && (data.cors.origins || []).length > 0} />
          <Row label="CORS wildcard" value={data.cors.wildcard ? "ENABLED — UNSAFE" : "disabled"}
               ok={!data.cors.wildcard} />
          <Row label="CORS regex fallback (preview)" value={data.cors.regex_fallback ? "yes (dev only)" : "off"} />
        </section>

        {/* Providers */}
        <section className="border border-zinc-800 bg-[#121212] rounded-sm p-5" data-testid="diag-providers">
          <h2 className="text-sm font-semibold text-white mb-3 flex items-center gap-2">
            <Database size={14} className="text-[#FFB020]" /> Providers
          </h2>
          {Object.entries(data.providers).map(([key, p]) => (
            <Row
              key={key}
              label={key.replace(/_/g, " ")}
              value={
                <span className="flex items-center gap-2">
                  <span
                    className="font-mono text-[10px] uppercase tracking-widest px-1.5 py-0.5 rounded-sm border"
                    style={
                      p.mode === "live"
                        ? { color: "#00FF66", background: "rgba(0,255,102,0.1)", borderColor: "#00FF66" }
                        : p.mode === "mock"
                        ? { color: "#FFB020", background: "rgba(255,176,32,0.1)", borderColor: "#FFB020" }
                        : { color: "#71717A", borderColor: "#71717A" }
                    }
                  >
                    {p.mode}
                  </span>
                  <span className="font-mono text-[11px] text-zinc-500">{p.provider}{p.model ? ` · ${p.model}` : ""}</span>
                </span>
              }
            />
          ))}
        </section>

        {/* Storage */}
        <section className="border border-zinc-800 bg-[#121212] rounded-sm p-5" data-testid="diag-storage">
          <h2 className="text-sm font-semibold text-white mb-3 flex items-center gap-2">
            <HardDrive size={14} className="text-[#00FF66]" /> Storage
          </h2>
          <Row label="Mode" value={
            <span className="flex items-center gap-2">
              <span
                className="font-mono text-[10px] uppercase tracking-widest px-1.5 py-0.5 rounded-sm border"
                style={
                  data.storage.mode === "object"
                    ? { color: "#00FF66", background: "rgba(0,255,102,0.1)", borderColor: "#00FF66" }
                    : { color: "#FFB020", background: "rgba(255,176,32,0.1)", borderColor: "#FFB020" }
                }
              >
                {data.storage.mode}
              </span>
              {data.storage.bucket && (
                <span className="font-mono text-[11px] text-zinc-500">{data.storage.bucket}</span>
              )}
            </span>
          } ok={data.storage.mode === "object" || data.dev_mode} />
          {data.storage.public_url_strategy && (
            <Row label="Public URL strategy" value={data.storage.public_url_strategy} />
          )}
          {data.storage.public_base_url && (
            <Row label="Public base URL" value={data.storage.public_base_url} />
          )}
          {data.storage.endpoint_url && (
            <Row label="Endpoint" value={data.storage.endpoint_url} />
          )}
          {data.storage.region && (
            <Row label="Region" value={data.storage.region} />
          )}
          {"credentials_present" in data.storage && (
            <Row label="Credentials present" value={data.storage.credentials_present ? "yes" : "no"} ok={data.storage.credentials_present} />
          )}
          {"probe_ok" in data.storage && (
            <Row label="Live storage probe" value={data.storage.probe_ok ? `ok · ${data.storage.probe_latency_ms ?? "?"} ms` : (data.storage.probe_error || "failed")} ok={!!data.storage.probe_ok} />
          )}
          {data.storage.probed_at && (
            <Row label="Probed at" value={data.storage.probed_at} />
          )}
          <Row label="Retention" value={`${data.storage.retention_days} days`} />
          <Row label="Renders" value={`${fmtBytes(data.storage.renders.bytes)} · ${data.storage.renders.files} files`} />
          <Row label="Thumbnails" value={`${fmtBytes(data.storage.thumbnails.bytes)} · ${data.storage.thumbnails.files} files`} />
          <Row label="Audio" value={`${fmtBytes(data.storage.audio.bytes)} · ${data.storage.audio.files} files`} />
          {data.storage.warning && (
            <div className="mt-3 px-3 py-2 border border-[#FFB020]/30 bg-[#FFB020]/5 rounded-sm font-mono text-[10px] text-[#FFB020] uppercase tracking-widest">
              {data.storage.warning}
            </div>
          )}
        </section>

        {/* Render queue + counts */}
        <section className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div className="border border-zinc-800 bg-[#121212] rounded-sm p-5" data-testid="diag-render">
            <h2 className="text-sm font-semibold text-white mb-3">Render queue</h2>
            <Row label="Active jobs" value={String(data.render_queue.active_jobs)} />
            <Row label="Concurrency" value={data.render_queue.concurrency} />
            <Row label="Lock" value={data.render_queue.lock} />
            <Row label="Timeout" value={`${data.render_queue.timeout_seconds}s`} />
          </div>
          <div className="border border-zinc-800 bg-[#121212] rounded-sm p-5" data-testid="diag-counts">
            <h2 className="text-sm font-semibold text-white mb-3">Data</h2>
            <Row label="Users" value={String(data.data_counts.users)} />
            <Row label="Projects" value={String(data.data_counts.projects)} />
          </div>
        </section>
      </div>
    </AppShell>
  );
}
