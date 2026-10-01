import React, { useEffect, useState } from "react";
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid, PieChart, Pie, Cell, LineChart, Line } from "recharts";
import AppShell from "../components/AppShell";
import TopBar from "../components/TopBar";
import { api } from "../lib/api";
import { STATUS_META } from "../lib/format";

const PIE_COLOURS = ["#00E5FF", "#7B61FF", "#00FF66", "#FFB020", "#FF3366", "#A1A1AA", "#52525B", "#3F3F46"];

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
        <div className="p-8 text-sm text-zinc-500 font-mono">Loading…</div>
      </AppShell>
    );
  }

  const statusData = Object.entries(a.status_counts).map(([k, v]) => ({
    status: STATUS_META[k]?.label || k,
    count: v,
    fill: STATUS_META[k]?.color || "#00E5FF",
  }));
  const niches = Object.entries(a.niche_counts).map(([name, value]) => ({ name, value }));

  return (
    <AppShell>
      <TopBar title="Analytics" subtitle="Studio intelligence" />
      <div className="p-8 space-y-6">
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
          <Stat label="Projects" value={a.total_projects} />
          <Stat label="Completed" value={a.completed} color="#00FF66" />
          <Stat label="Avg quality" value={Math.round(a.average_quality_score)} unit="/100" color="#7B61FF" />
          <Stat label="Total cost" value={`$${a.total_estimated_cost.toFixed(2)}`} color="#FFB020" />
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
          <div className="lg:col-span-2 border border-zinc-800 bg-[#121212] p-5 rounded-sm">
            <h3 className="text-sm font-semibold mb-4">Videos by status</h3>
            <div style={{ height: 260 }}>
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={statusData}>
                  <CartesianGrid vertical={false} stroke="#27272A" />
                  <XAxis dataKey="status" stroke="#52525B" fontSize={10} tickLine={false} axisLine={{ stroke: "#27272A" }} />
                  <YAxis stroke="#52525B" fontSize={10} tickLine={false} axisLine={{ stroke: "#27272A" }} allowDecimals={false} />
                  <Tooltip contentStyle={{ background: "#0A0A0A", border: "1px solid #27272A", borderRadius: 0, fontSize: 12 }} cursor={{ fill: "rgba(0,229,255,0.05)" }} />
                  <Bar dataKey="count" />
                </BarChart>
              </ResponsiveContainer>
            </div>
          </div>
          <div className="border border-zinc-800 bg-[#121212] p-5 rounded-sm">
            <h3 className="text-sm font-semibold mb-4">Most used niches</h3>
            <div style={{ height: 260 }}>
              <ResponsiveContainer width="100%" height="100%">
                <PieChart>
                  <Pie data={niches} dataKey="value" nameKey="name" innerRadius={45} outerRadius={80} stroke="#0A0A0A" strokeWidth={2}>
                    {niches.map((_, i) => <Cell key={i} fill={PIE_COLOURS[i % PIE_COLOURS.length]} />)}
                  </Pie>
                  <Tooltip contentStyle={{ background: "#0A0A0A", border: "1px solid #27272A", borderRadius: 0, fontSize: 12 }} />
                </PieChart>
              </ResponsiveContainer>
            </div>
            <div className="mt-3 space-y-1">
              {niches.map((n, i) => (
                <div key={n.name} className="flex items-center justify-between text-xs">
                  <span className="flex items-center gap-2">
                    <span className="w-2.5 h-2.5" style={{ background: PIE_COLOURS[i % PIE_COLOURS.length] }} />
                    <span className="text-zinc-300">{n.name}</span>
                  </span>
                  <span className="font-mono text-zinc-500">{n.value}</span>
                </div>
              ))}
            </div>
          </div>
        </div>

        <div className="border border-zinc-800 bg-[#121212] p-5 rounded-sm">
          <h3 className="text-sm font-semibold mb-4">Projects created over time</h3>
          <div style={{ height: 200 }}>
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={a.projects_over_time}>
                <CartesianGrid vertical={false} stroke="#27272A" />
                <XAxis dataKey="date" stroke="#52525B" fontSize={10} tickLine={false} axisLine={{ stroke: "#27272A" }} />
                <YAxis stroke="#52525B" fontSize={10} tickLine={false} axisLine={{ stroke: "#27272A" }} allowDecimals={false} />
                <Tooltip contentStyle={{ background: "#0A0A0A", border: "1px solid #27272A", borderRadius: 0, fontSize: 12 }} />
                <Line type="monotone" dataKey="count" stroke="#00E5FF" strokeWidth={2} dot={{ r: 3, fill: "#00E5FF" }} />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </div>
      </div>
    </AppShell>
  );
}

function Stat({ label, value, unit, color = "#00E5FF" }) {
  return (
    <div className="border border-zinc-800 bg-[#121212] p-5 rounded-sm">
      <div className="font-mono text-[10px] uppercase tracking-widest text-zinc-500 mb-4">{label}</div>
      <div className="flex items-baseline gap-2">
        <span className="font-mono text-3xl font-semibold tabular-nums" style={{ color }}>{value}</span>
        {unit && <span className="font-mono text-xs text-zinc-500">{unit}</span>}
      </div>
    </div>
  );
}
