import React, { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ArrowUpRight, Plus, FileVideo, CheckCircle2, Activity, DollarSign } from "lucide-react";
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid, LineChart, Line } from "recharts";
import AppShell from "../components/AppShell";
import TopBar from "../components/TopBar";
import ProjectCard from "../components/ProjectCard";
import { api } from "../lib/api";
import { formatCurrency, STATUS_META } from "../lib/format";

function StatCard({ label, value, accent = "#00E5FF", icon: Icon, unit, delay = 0 }) {
  return (
    <div
      className={`border border-zinc-800 bg-[#121212] p-5 rounded-sm ff-rise`}
      style={{ animationDelay: `${delay}ms` }}
    >
      <div className="flex items-center justify-between mb-4">
        <span className="font-mono text-[10px] uppercase tracking-widest text-zinc-500">{label}</span>
        {Icon && <Icon size={14} strokeWidth={1.5} style={{ color: accent }} />}
      </div>
      <div className="flex items-baseline gap-2">
        <span className="font-mono text-3xl font-semibold tabular-nums" style={{ color: accent }}>
          {value}
        </span>
        {unit && <span className="font-mono text-xs text-zinc-500">{unit}</span>}
      </div>
    </div>
  );
}

export default function DashboardPage() {
  const [analytics, setAnalytics] = useState(null);
  const [projects, setProjects] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    (async () => {
      try {
        const [a, p] = await Promise.all([api.get("/analytics/overview"), api.get("/projects")]);
        setAnalytics(a.data);
        setProjects(p.data);
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  const statusData = analytics
    ? Object.entries(analytics.status_counts).map(([k, v]) => ({
        status: STATUS_META[k]?.label || k,
        count: v,
        fill: STATUS_META[k]?.color || "#00E5FF",
      }))
    : [];

  const timeData = analytics?.projects_over_time || [];

  return (
    <AppShell>
      <TopBar
        title="Dashboard"
        subtitle="Control Room"
        right={
          <Link
            to="/app/projects/new"
            data-testid="dashboard-create-project"
            className="flex items-center gap-2 text-xs font-semibold bg-[#00E5FF] text-black px-3 py-2 rounded-sm hover:bg-[#33EFFF] transition-colors"
          >
            <Plus size={14} strokeWidth={2} /> New Project
          </Link>
        }
      />

      <div className="p-8 space-y-8">
        {/* Metrics row */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <StatCard label="Total Projects" value={analytics?.total_projects ?? "—"} icon={FileVideo} delay={0} />
          <StatCard label="Completed" value={analytics?.completed ?? "—"} accent="#00FF66" icon={CheckCircle2} delay={40} />
          <StatCard label="In Progress" value={analytics?.in_progress ?? "—"} accent="#FFB020" icon={Activity} delay={80} />
          <StatCard
            label="Avg Quality"
            value={analytics ? Math.round(analytics.average_quality_score) : "—"}
            unit="/100"
            accent="#7B61FF"
            delay={120}
          />
        </div>

        {/* Charts row */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
          <div className="lg:col-span-2 border border-zinc-800 bg-[#121212] p-5 rounded-sm">
            <div className="flex items-center justify-between mb-6">
              <h3 className="text-sm font-semibold">Production Status</h3>
              <span className="font-mono text-[10px] text-zinc-500 uppercase tracking-widest">
                live
              </span>
            </div>
            <div style={{ width: "100%", height: 240 }}>
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={statusData} margin={{ top: 10, right: 0, left: -10, bottom: 0 }}>
                  <CartesianGrid vertical={false} stroke="#27272A" />
                  <XAxis dataKey="status" stroke="#52525B" fontSize={10} tickLine={false} axisLine={{ stroke: "#27272A" }} />
                  <YAxis stroke="#52525B" fontSize={10} tickLine={false} axisLine={{ stroke: "#27272A" }} allowDecimals={false} />
                  <Tooltip
                    contentStyle={{ background: "#0A0A0A", border: "1px solid #27272A", borderRadius: 0, fontSize: 12 }}
                    cursor={{ fill: "rgba(0,229,255,0.05)" }}
                  />
                  <Bar dataKey="count" />
                </BarChart>
              </ResponsiveContainer>
            </div>
          </div>

          <div className="border border-zinc-800 bg-[#121212] p-5 rounded-sm space-y-5">
            <div>
              <span className="font-mono text-[10px] uppercase tracking-widest text-zinc-500">
                Est. monthly output
              </span>
              <div className="mt-2 font-mono text-3xl font-semibold text-[#00E5FF]">
                {analytics?.monthly_output_projection ?? "—"}
                <span className="text-zinc-600 text-base"> videos</span>
              </div>
            </div>
            <div className="pt-4 border-t border-zinc-800">
              <span className="font-mono text-[10px] uppercase tracking-widest text-zinc-500">
                Total est. cost
              </span>
              <div className="mt-2 flex items-center gap-2 font-mono text-xl text-[#FFB020]">
                <DollarSign size={16} strokeWidth={1.5} />
                {analytics ? analytics.total_estimated_cost.toFixed(2) : "—"}
              </div>
            </div>
            <div className="pt-4 border-t border-zinc-800" style={{ width: "100%", height: 100 }}>
              <span className="font-mono text-[10px] uppercase tracking-widest text-zinc-500 block mb-2">
                Created over time
              </span>
              <div style={{ width: "100%", height: 60 }}>
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={timeData}>
                    <Line type="monotone" dataKey="count" stroke="#7B61FF" strokeWidth={2} dot={false} />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            </div>
          </div>
        </div>

        {/* Recent projects */}
        <section>
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-sm font-semibold">Recent projects</h2>
            <Link
              to="/app/projects"
              data-testid="view-all-projects"
              className="flex items-center gap-1 text-xs text-zinc-400 hover:text-[#00E5FF]"
            >
              View all <ArrowUpRight size={12} strokeWidth={1.5} />
            </Link>
          </div>
          {loading ? (
            <div className="text-sm text-zinc-500 font-mono">Loading…</div>
          ) : projects.length === 0 ? (
            <div className="border border-zinc-800 border-dashed p-10 text-center rounded-sm">
              <p className="text-sm text-zinc-400 mb-4">No projects yet.</p>
              <Link
                to="/app/projects/new"
                className="inline-flex items-center gap-2 bg-[#00E5FF] text-black px-4 py-2 text-sm font-semibold rounded-sm"
              >
                <Plus size={14} strokeWidth={2} /> Create your first project
              </Link>
            </div>
          ) : (
            <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
              {projects.slice(0, 6).map((p, i) => (
                <ProjectCard key={p.id} project={p} index={i} />
              ))}
            </div>
          )}
        </section>
      </div>
    </AppShell>
  );
}
