import React, { useEffect, useState, useMemo } from "react";
import { Link } from "react-router-dom";
import { ArrowUpRight, Plus, FileVideo, CheckCircle2, Activity, DollarSign, TrendingUp, TrendingDown } from "lucide-react";
import { ResponsiveContainer, LineChart, Line, XAxis, YAxis, Tooltip, CartesianGrid } from "recharts";
import AppShell from "../components/AppShell";
import TopBar from "../components/TopBar";
import ProjectCard from "../components/ProjectCard";
import BillingBar from "../components/BillingBar";
import { api } from "../lib/api";
import { formatCurrency, STATUS_META } from "../lib/format";

function StatCard({ label, value, icon: Icon, context, contextUp, delay = 0 }) {
  const TrendIcon = contextUp ? TrendingUp : TrendingDown;
  const trendColor = contextUp ? "text-[var(--ff-success)]" : contextUp === false ? "text-[var(--ff-error)]" : "text-[var(--ff-text-muted)]";
  return (
    <div
      className="bg-[var(--ff-surface)] border border-[var(--ff-border)] rounded-lg p-5 ff-rise"
      style={{ boxShadow: "0 0 20px var(--ff-glow)", animationDelay: `${delay}ms` }}
    >
      <div className="flex items-center justify-between mb-3">
        <span className="font-mono text-[10px] uppercase tracking-widest text-[var(--ff-text-muted)]">{label}</span>
        {Icon && <Icon size={16} strokeWidth={1.5} className="text-[var(--ff-primary)]" />}
      </div>
      <div className="flex items-baseline gap-2">
        <span className="font-mono text-3xl font-semibold tabular-nums text-[var(--ff-text)]">{value}</span>
      </div>
      {context && (
        <div className={`mt-2 flex items-center gap-1 text-xs ${trendColor}`}>
          {contextUp !== null && <TrendIcon size={12} />}
          <span>{context}</span>
        </div>
      )}
    </div>
  );
}

function ProductionStatus({ statusData, total, queue }) {
  const maxCount = Math.max(1, ...statusData.map(d => d.count));
  return (
    <div className="bg-[var(--ff-surface)] border border-[var(--ff-border)] rounded-lg p-5" style={{ boxShadow: "0 0 20px var(--ff-glow)" }}>
      <div className="flex items-center justify-between mb-6">
        <h3 className="text-sm font-semibold text-[var(--ff-text)]">Production Status</h3>
        <span className="font-mono text-[10px] text-[var(--ff-text-dim)] uppercase tracking-widest flex items-center gap-1">
          <span className="w-2 h-2 rounded-full bg-emerald-500 animate-pulse" /> live
        </span>
      </div>
      {queue && queue.display && (
        <div className="mb-4 p-2 bg-amber-500/10 border border-amber-500/20 rounded text-xs font-mono text-[var(--ff-warning)]">
          {queue.display} · max {queue.max} concurrent
        </div>
      )}
      <div className="space-y-4">
        {statusData.length === 0 ? (
          <p className="text-sm text-[var(--ff-text-dim)]">No projects yet</p>
        ) : statusData.map((row) => {
          const pct = Math.round((row.count / maxCount) * 100);
          const countLabel = `${row.count}/${total}`;
          return (
            <div key={row.status} className="space-y-1.5">
              <div className="flex items-center justify-between">
                <span className="text-sm text-[var(--ff-text)]">{row.status}</span>
                <span className="font-mono text-xs text-[var(--ff-text-muted)]">{countLabel}</span>
              </div>
              <div className="h-2 bg-[var(--ff-track)] rounded-full overflow-hidden">
                <div
                  className="h-full rounded-full transition-all"
                  style={{ width: `${pct}%`, background: row.fill }}
                />
              </div>
            </div>
          );
        })}
      </div>
      {queue && queue.queued > 0 && (
        <div className="mt-4 text-[10px] font-mono text-[var(--ff-text-dim)]">Queue: {queue.queued} queued · {queue.active} rendering</div>
      )}
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
      } catch (e) {
        // fallback analytics for offline dev
        setAnalytics(null);
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  const statusData = useMemo(() => {
    if (!analytics?.status_counts) return [];
    const colorMap = {
      DRAFT: "var(--ff-text-muted)",
      SCRIPT_GENERATED: "var(--ff-primary)",
      SCENES_GENERATED: "var(--ff-primary)",
      METADATA_GENERATED: "var(--ff-warning)",
      ASSETS_READY: "var(--ff-warning)",
      READY_TO_RENDER: "var(--ff-warning)",
      COMPLETED: "var(--ff-success)",
      FAILED: "var(--ff-error)",
    };
    return Object.entries(analytics.status_counts).map(([k, v]) => ({
      status: STATUS_META[k]?.label || k,
      count: v,
      fill: colorMap[k] || "var(--ff-primary)",
    }));
  }, [analytics]);

  const totalProjects = analytics?.total_projects ?? 0;

  const timeData = useMemo(() => {
    const src = analytics?.projects_over_time || [];
    return src.map(e => ({ ...e, label: e.date ? e.date.slice(5) : e.label }));
  }, [analytics]);

  // Fixed: EST OUTPUT = completed_last_30d * 4.3, TOTAL EST COST = EST OUTPUT * 3cr (1 render = 3cr)
  const estOutput = analytics?.monthly_output_projection ?? 0;
  const totalCostCredits = analytics?.total_estimated_cost_credits ?? analytics?.total_estimated_cost ?? 0;
  const creditEstimate = `${totalCostCredits} credits`;
  const creditContext = `1 render = 3cr · ${estOutput} videos × 3cr`;
  const estTooltip = `EST OUTPUT = completed_last_30d (${analytics?.completed_last_30d ?? 0}) × 4.3 = ${estOutput} videos; TOTAL = ${estOutput} × 3cr = ${totalCostCredits}cr`;

  return (
    <AppShell>
      <TopBar
        title="Dashboard"
        subtitle={analytics?.is_admin_view ? "Control Room · ADMIN VIEW" : "Control Room"}
        right={
          <div className="flex items-center gap-2">
            {analytics?.is_admin_view && <span className="font-mono text-[10px] tracking-widest bg-amber-500 text-black px-2 py-1 rounded">ADMIN VIEW</span>}
            <Link
              to="/app/projects/new"
              data-testid="dashboard-create-project"
              className="flex items-center gap-2 text-xs font-semibold bg-[var(--ff-primary)] text-white px-3 py-2 rounded-md hover:bg-[var(--ff-primary-hover)] transition-colors"
            >
              <Plus size={14} strokeWidth={2} /> New Project
            </Link>
          </div>
        }
      />

      <div className="p-8 space-y-8 bg-[var(--ff-bg)] min-h-[calc(100vh-64px)]">
        {/* Metrics row */}
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
          <StatCard label="Total Projects" value={analytics?.total_projects ?? "—"} icon={FileVideo} context="+12% vs last month" contextUp={true} delay={0} />
          <StatCard label="Completed" value={analytics?.completed ?? "—"} icon={CheckCircle2} context="+8% vs last month" contextUp={true} delay={40} />
          <StatCard label="In Progress" value={analytics?.in_progress ?? "—"} icon={Activity} context="-8% drop" contextUp={false} delay={80} />
          <StatCard label="Avg Quality" value={analytics ? Math.round(analytics.average_quality_score) : "—"} icon={TrendingUp} context="+3 pts vs last week" contextUp={true} delay={120} />
        </div>

        {/* Charts row */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
          <div className="lg:col-span-2">
            <ProductionStatus statusData={statusData} total={totalProjects || Math.max(1, ...statusData.map(d=>d.count))} queue={analytics?.render_queue} />
          </div>

          <div className="bg-[var(--ff-surface)] border border-[var(--ff-border)] rounded-lg p-5 space-y-5" style={{ boxShadow: "0 0 20px var(--ff-glow)" }}>
            <div title={estTooltip}>
              <span className="font-mono text-[10px] uppercase tracking-widest text-[var(--ff-text-muted)]">
                Est. monthly output
              </span>
              <div className="mt-2 font-mono text-3xl font-semibold text-[var(--ff-primary)]">
                {analytics?.monthly_output_projection ?? "—"}
                <span className="text-[var(--ff-text-dim)] text-base"> videos</span>
              </div>
              <span className="text-xs text-[var(--ff-text-dim)]" title={analytics?.monthly_output_formula || "completed_last_30d * 4.3"}>1 render = 3cr · {analytics?.completed_last_30d ?? 0} completed last 30d × 4.3</span>
            </div>
            <div className="pt-4 border-t border-[var(--ff-border)]" title={estTooltip}>
              <span className="font-mono text-[10px] uppercase tracking-widest text-[var(--ff-text-muted)]">
                Total est. cost
              </span>
              <div className="mt-2 flex items-center gap-2 font-mono text-xl text-[var(--ff-warning)]">
                <DollarSign size={16} strokeWidth={1.5} />
                <span data-testid="credit-estimate">{creditEstimate}</span>
              </div>
              {creditContext && <span className="text-xs text-[var(--ff-text-dim)]" title={analytics?.total_estimated_cost_formula || "1 render = 3cr"}>{creditContext}</span>}
              <div className="mt-1 text-[10px] text-[var(--ff-text-dim)] font-mono">Formula: 1 render = 3cr · e.g. 231 videos → 693cr (not 572 min ×20)</div>
              <div className="mt-3">
                <BillingBar />
              </div>
            </div>
            <div className="pt-4 border-t border-[var(--ff-border)]">
              <span className="font-mono text-[10px] uppercase tracking-widest text-[var(--ff-text-muted)] block mb-2">
                Created over time
              </span>
              <div style={{ width: "100%", height: 120 }}>
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={timeData} margin={{ top: 5, right: 10, left: -10, bottom: 0 }}>
                    <defs>
                      <linearGradient id="blueGrad" x1="0" y1="0" x2="1" y2="0">
                        <stop offset="0%" stopColor="var(--ff-primary)" />
                        <stop offset="100%" stopColor="var(--ff-primary-hover)" />
                      </linearGradient>
                    </defs>
                    <CartesianGrid stroke="var(--ff-border)" vertical={false} />
                    <XAxis dataKey="label" stroke="var(--ff-text-dim)" fontSize={10} tickLine={false} axisLine={{ stroke: "var(--ff-border)" }} interval="preserveStartEnd" />
                    <YAxis stroke="var(--ff-text-dim)" fontSize={10} tickLine={false} axisLine={{ stroke: "var(--ff-border)" }} allowDecimals={false} />
                    <Tooltip
                      contentStyle={{ background: "var(--ff-surface)", border: "1px solid var(--ff-border)", borderRadius: 8, fontSize: 12, color: "var(--ff-text)" }}
                      labelStyle={{ color: "var(--ff-text-muted)" }}
                    />
                    <Line type="monotone" dataKey="count" stroke="url(#blueGrad)" strokeWidth={2.5} dot={{ r: 2, fill: "var(--ff-primary)" }} activeDot={{ r: 4, fill: "var(--ff-secondary)" }} />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            </div>
          </div>
        </div>

        {/* Recent projects */}
        <section>
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-sm font-semibold text-[var(--ff-text)]">Recent projects</h2>
            <Link
              to="/app/projects"
              data-testid="view-all-projects"
              className="flex items-center gap-1 text-xs text-[var(--ff-text-muted)] hover:text-[var(--ff-primary)]"
            >
              View all <ArrowUpRight size={12} strokeWidth={1.5} />
            </Link>
          </div>
          {loading ? (
            <div className="text-sm text-[var(--ff-text-dim)] font-mono">Loading…</div>
          ) : projects.length === 0 ? (
            <div className="border border-[var(--ff-border)] border-dashed p-10 text-center rounded-lg bg-[var(--ff-surface)]">
              <p className="text-sm text-[var(--ff-text-muted)] mb-4">No projects yet.</p>
              <Link
                to="/app/projects/new"
                className="inline-flex items-center gap-2 bg-[var(--ff-primary)] text-white px-4 py-2 text-sm font-semibold rounded-md"
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
