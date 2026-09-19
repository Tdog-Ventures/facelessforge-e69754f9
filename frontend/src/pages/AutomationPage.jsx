import React, { useEffect, useState } from "react";
import { toast } from "sonner";
import { Loader2, Youtube, Clock, Play, Power, Zap } from "lucide-react";
import AppShell from "../components/AppShell";
import TopBar from "../components/TopBar";
import { api, formatApiError } from "../lib/api";

export default function AutomationPage() {
  const [status, setStatus] = useState(null);
  const [ytStatus, setYtStatus] = useState(null);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [toggling, setToggling] = useState(false);
  const [countdown, setCountdown] = useState("");
  const [selectedNiche, setSelectedNiche] = useState("AI side hustles");
  const [scheduleTime] = useState("09:00 Australia/Adelaide");

  const fetchAll = async () => {
    try {
      const [s, y] = await Promise.all([
        api.get("/forge/status"),
        api.get("/youtube/status").catch(() => ({ data: { connected: false } })),
      ]);
      setStatus(s.data);
      setYtStatus(y.data);
      if (s.data?.last_run?.niche) setSelectedNiche(s.data.last_run.niche);
    } catch (e) {
      // fallback
      setStatus((prev) => prev || { enabled: true, next_run: new Date(Date.now()+86400000).toISOString(), niches: ["AI side hustles","Faceless automation"] });
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { fetchAll(); }, []);
  useEffect(() => {
    if (!status?.next_run) return;
    const iv = setInterval(() => {
      const nxt = new Date(status.next_run);
      const diff = nxt - new Date();
      if (diff <= 0) { setCountdown("Due now"); return; }
      const h = Math.floor(diff/3600000);
      const m = Math.floor((diff%3600000)/60000);
      const s = Math.floor((diff%60000)/1000);
      setCountdown(`${h}h ${m}m ${s}s`);
    }, 1000);
    return () => clearInterval(iv);
  }, [status?.next_run]);

  const toggle = async () => {
    setToggling(true);
    const next = !status.enabled;
    try {
      const { data } = await api.post("/forge/toggle", null, { params: { enabled: next } });
      setStatus({ ...status, enabled: data.auto_post_enabled, auto_post_enabled: data.auto_post_enabled });
      toast.success(next ? "Auto-post enabled" : "Auto-post disabled");
    } catch (e) {
      toast.error("Toggle failed", { description: formatApiError(e.response?.data?.detail) });
    } finally { setToggling(false); }
  };

  const runNow = async () => {
    setRunning(true);
    try {
      toast.info("Forging 1 video — this takes ~2-4 minutes");
      const { data } = await api.post("/forge/run-now", null, { params: { topic: undefined, niche: selectedNiche } });
      toast.success(`Forge done — ${data.youtube_url || data.youtube_video_id || "uploaded"}`);
      await fetchAll();
    } catch (e) {
      toast.error("Forge failed", { description: formatApiError(e.response?.data?.detail) || e.message });
    } finally { setRunning(false); }
  };

  const connectYoutube = async () => {
    try {
      const { data } = await api.get("/youtube/auth");
      if (data.auth_url) window.open(data.auth_url, "_blank");
    } catch (e) {
      toast.error("YouTube auth failed", { description: formatApiError(e.response?.data?.detail) });
    }
  };

  if (loading) {
    return <AppShell><TopBar title="Automation" /><div className="p-8 text-sm text-zinc-500 font-mono">Loading automation…</div></AppShell>;
  }

  const enabled = status?.enabled ?? status?.auto_post_enabled ?? true;
  const lastRun = status?.last_run;
  const recent = status?.recent_runs || [];

  return (
    <AppShell>
      <TopBar title="Automation" subtitle="FacelessForge Auto-Post Loop — Monetisation Ready" />
      <div className="p-8 max-w-4xl space-y-6">
        {/* Toggle + Schedule + Niche */}
        <div className="border border-zinc-800 bg-[#121212] p-6 rounded-sm space-y-6">
          <div className="flex items-center justify-between">
            <div>
              <h3 className="text-sm font-semibold text-white flex items-center gap-2"><Zap size={14} className="text-[#00E5FF]" /> Auto-post Loop</h3>
              <p className="font-mono text-[10px] text-zinc-500 mt-1">Daily 09:00 Australia/Adelaide — Topic → Project → Script → Render → YouTube → Shorts</p>
            </div>
            <button
              data-testid="automation-toggle"
              onClick={toggle}
              disabled={toggling}
              className={`flex items-center gap-2 px-4 py-2 rounded-sm font-mono text-xs tracking-widest uppercase border transition-colors ${enabled ? "bg-[#00E5FF] text-black border-[#00E5FF]" : "bg-zinc-900 text-zinc-400 border-zinc-700 hover:border-zinc-500"}`}
            >
              <Power size={14} /> {enabled ? "ON" : "OFF"}
            </button>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            <div>
              <label className="font-mono text-[10px] uppercase tracking-widest text-zinc-500 mb-2 block">Schedule time</label>
              <div className="flex items-center gap-2 bg-[#0A0A0A] border border-zinc-800 px-3 py-2.5 rounded-sm font-mono text-sm text-zinc-300">
                <Clock size={14} className="text-zinc-500" /> {scheduleTime}
              </div>
            </div>
            <div>
              <label className="font-mono text-[10px] uppercase tracking-widest text-zinc-500 mb-2 block">Niche selector</label>
              <select
                data-testid="niche-selector"
                value={selectedNiche}
                onChange={(e) => setSelectedNiche(e.target.value)}
                className="w-full bg-[#0A0A0A] border border-zinc-800 px-3 py-2.5 text-sm rounded-sm focus:border-[#00E5FF] text-white"
              >
                {(status?.niches || ["AI side hustles","Faceless automation"]).map((n) => (
                  <option key={n} value={n}>{n}</option>
                ))}
              </select>
            </div>
            <div>
              <label className="font-mono text-[10px] uppercase tracking-widest text-zinc-500 mb-2 block">YouTube channel</label>
              <div data-testid="yt-connected-badge" className={`flex items-center gap-2 px-3 py-2.5 rounded-sm border font-mono text-xs ${ytStatus?.connected ? "bg-green-900/20 border-green-800 text-green-400" : "bg-red-900/20 border-red-800 text-red-400"}`}>
                <Youtube size={14} /> {ytStatus?.connected ? "Connected" : "Not connected"}
                {!ytStatus?.connected && <button onClick={connectYoutube} className="ml-auto underline">Connect</button>}
              </div>
            </div>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4 pt-2">
            <div className="bg-[#0A0A0A] border border-zinc-800 p-4 rounded-sm">
              <div className="font-mono text-[10px] uppercase tracking-widest text-zinc-500">Next run countdown</div>
              <div data-testid="next-run-countdown" className="font-mono text-lg text-white mt-1">{countdown || "—"}</div>
              <div className="font-mono text-[10px] text-zinc-600 mt-1">{status?.next_run ? new Date(status.next_run).toLocaleString("en-AU", { timeZone: "Australia/Adelaide" }) + " Adelaide" : ""}</div>
            </div>
            <div className="bg-[#0A0A0A] border border-zinc-800 p-4 rounded-sm">
              <div className="font-mono text-[10px] uppercase tracking-widest text-zinc-500">Last run log</div>
              <div data-testid="last-run-log" className="font-mono text-xs text-zinc-300 mt-2 max-h-24 overflow-auto whitespace-pre-wrap">
                {lastRun ? (Array.isArray(lastRun.log) ? lastRun.log.join("\n") : JSON.stringify(lastRun, null, 2).slice(0,800)) : "No runs yet"}
              </div>
              {lastRun?.youtube_url && <a href={lastRun.youtube_url} target="_blank" rel="noreferrer" className="font-mono text-[10px] text-[#00E5FF] underline">{lastRun.youtube_url}</a>}
            </div>
          </div>

          <button
            data-testid="run-now-btn"
            onClick={runNow}
            disabled={running}
            className="w-full flex items-center justify-center gap-2 bg-[#00E5FF] text-black font-bold text-sm px-6 py-3 rounded-sm hover:bg-[#33EFFF] disabled:opacity-60 transition-colors"
          >
            {running ? <Loader2 size={16} className="animate-spin" /> : <Play size={16} />}
            {running ? "FORGING…" : "RUN NOW - FORGE 1 VIDEO"}
          </button>

          {recent.length > 0 && (
            <div className="border-t border-zinc-800 pt-4">
              <div className="font-mono text-[10px] uppercase tracking-widest text-zinc-500 mb-2">Recent runs</div>
              <div className="space-y-1">
                {recent.slice(0,5).map((r) => (
                  <div key={r.id} className="flex items-center justify-between font-mono text-[11px] bg-[#0A0A0A] border border-zinc-800 px-3 py-2 rounded-sm">
                    <span className="text-zinc-300 truncate">{r.project_id?.slice(0,8)} — {r.youtube_video_id || r.status}</span>
                    <span className={`${r.status==="completed"?"text-green-400": r.status==="failed"?"text-red-400":"text-zinc-500"}`}>{r.status}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>
    </AppShell>
  );
}
