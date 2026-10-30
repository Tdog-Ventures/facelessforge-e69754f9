import React, { useEffect, useState } from "react";
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid, PieChart, Pie, Cell, LineChart, Line } from "recharts";
import AppShell from "../components/AppShell";
import TopBar from "../components/TopBar";
import { api } from "../lib/api";
import { STATUS_META } from "../lib/format";

const PIE_COLOURS = ["var(--ff-accent)", "var(--ff-accent-2)", "var(--ff-success)", "var(--ff-warning)", "var(--ff-error)", "var(--ff-text-dim)", "var(--ff-track)", "var(--ff-border-strong)"];

export default function AnalyticsPage() {
  const [a, setA] = useState(null);
  useEffect(() => {
    (async () => {
      try {
        const { data } = await api.get("/analytics/overview");
        setA(data);
      } catch {/* ignore */}
    })();
  }, []);

  if (!a) {
    return (
      <AppShell>
        <TopBar title="Analytics" />
        <div className="p-8 text-sm text-[var(--ff-text-dim)] font-mono">Loading…</div>
      </AppShell>
    );
  }

  const statusData = Object.entries(a.status_counts).map(([k, v]) => ({
    status: STATUS_META[k]?.label || k,
    count: v,
    fill: STATUS_META[k]?.color || "var(--ff-accent)",
  }));
  const niches = Object.entries(a.niche_counts).map(([name, value]) => ({ name, value }));

  return (
    <AppShell>
      <TopBar title="Analytics" subtitle="Studio intelligence" />
      <div className="p-8 space-y-6">
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
          <Stat label="Projects" value={a.total_projects} />
          <Stat label="Completed" value={a.completed} color="var(--ff-success)" />
          <Stat label="Avg quality" value={Math.round(a.average_quality_score)} unit="/100" color="var(--ff-accent-2)" />
          <Stat label="Total cost" value={`$${a.total_estimated_cost.toFixed(2)}`} color="var(--ff-warning)" />
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
          <div className="lg:col-span-2 border border-[var(--ff-border)] bg-[var(--ff-surface)] p-5 rounded-sm">
            <h3 className="text-sm font-semibold mb-4">Videos by status</h3>
            <div style={{ height: 260 }}>
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={statusData}>
                  <CartesianGrid vertical={false} stroke="var(--ff-border)" />
                  <XAxis dataKey="status" stroke="var(--ff-text-dim)" fontSize={10} tickLine={false} axisLine={{ stroke: "var(--ff-border)" }} />
                  <YAxis stroke="var(--ff-text-dim)" fontSize={10} tickLine={false} axisLine={{ stroke: "var(--ff-border)" }} allowDecimals={false} />
                  <Tooltip contentStyle={{ background: "var(--ff-bg)", border: "1px solid var(--ff-border)", borderRadius: 0, fontSize: 12 }} cursor={{ fill: "rgba(0,229,255,0.05)" }} />
                  <Bar dataKey="count" />
                </BarChart>
              </ResponsiveContainer>
            </div>
          </div>
          <div className="border border-[var(--ff-border)] bg-[var(--ff-surface)] p-5 rounded-sm">
            <h3 className="text-sm font-semibold mb-4">Most used niches</h3>
            <div style={{ height: 260 }}>
              <ResponsiveContainer width="100%" height="100%">
                <PieChart>
                  <Pie data={niches} dataKey="value" nameKey="name" innerRadius={45} outerRadius={80} stroke="var(--ff-bg)" strokeWidth={2}>
                    {niches.map((_, i) => <Cell key={i} fill={PIE_COLOURS[i % PIE_COLOURS.length]} />)}
                  </Pie>
                  <Tooltip contentStyle={{ background: "var(--ff-bg)", border: "1px solid var(--ff-border)", borderRadius: 0, fontSize: 12 }} />
                </PieChart>
              </ResponsiveContainer>
            </div>
            <div className="mt-3 space-y-1">
              {niches.map((n, i) => (
                <div key={n.name} className="flex items-center justify-between text-xs">
                  <span className="flex items-center gap-2">
                    <span className="w-2.5 h-2.5" style={{ background: PIE_COLOURS[i % PIE_COLOURS.length] }} />
                    <span className="text-[var(--ff-text)]">{n.name}</span>
                  </span>
                  <span className="font-mono text-[var(--ff-text-dim)]">{n.value}</span>
                </div>
              ))}
            </div>
          </div>
        </div>

        <div className="border border-[var(--ff-border)] bg-[var(--ff-surface)] p-5 rounded-sm">
          <h3 className="text-sm font-semibold mb-4">Projects created over time</h3>
          <div style={{ height: 200 }}>
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={a.projects_over_time}>
                <CartesianGrid vertical={false} stroke="var(--ff-border)" />
                <XAxis dataKey="date" stroke="var(--ff-text-dim)" fontSize={10} tickLine={false} axisLine={{ stroke: "var(--ff-border)" }} />
                <YAxis stroke="var(--ff-text-dim)" fontSize={10} tickLine={false} axisLine={{ stroke: "var(--ff-border)" }} allowDecimals={false} />
                <Tooltip contentStyle={{ background: "var(--ff-bg)", border: "1px solid var(--ff-border)", borderRadius: 0, fontSize: 12 }} />
                <Line type="monotone" dataKey="count" stroke="var(--ff-accent)" strokeWidth={2} dot={{ r: 3, fill: "var(--ff-accent)" }} />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </div>
      </div>
    </AppShell>
  );
}

function Stat({ label, value, unit, color = "var(--ff-accent)" }) {
  return (
    <div className="border border-[var(--ff-border)] bg-[var(--ff-surface)] p-5 rounded-sm">
      <div className="font-mono text-[10px] uppercase tracking-widest text-[var(--ff-text-dim)] mb-4">{label}</div>
      <div className="flex items-baseline gap-2">
        <span className="font-mono text-3xl font-semibold tabular-nums" style={{ color }}>{value}</span>
        {unit && <span className="font-mono text-xs text-[var(--ff-text-dim)]">{unit}</span>}
      </div>
    </div>
  );
}
