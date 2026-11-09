import React, { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Plus, Search } from "lucide-react";
import AppShell from "../components/AppShell";
import TopBar from "../components/TopBar";
import ProjectCard from "../components/ProjectCard";
import { api } from "../lib/api";
import { STATUS_META } from "../lib/format";

export default function ProjectsPage() {
  const [projects, setProjects] = useState([]);
  const [loading, setLoading] = useState(true);
  const [q, setQ] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");

  useEffect(() => {
    (async () => {
      try {
        const { data } = await api.get("/projects");
        setProjects(data);
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  const filtered = projects.filter((p) => {
    if (statusFilter !== "all" && p.status !== statusFilter) return false;
    if (q && !`${p.name} ${p.niche} ${p.topic}`.toLowerCase().includes(q.toLowerCase())) return false;
    return true;
  });

  return (
    <AppShell>
      <TopBar
        title="Projects"
        subtitle={`${projects.length} total`}
        right={
          <Link
            to="/app/projects/new"
            data-testid="projects-create"
            className="flex items-center gap-2 text-xs font-semibold bg-[#00E5FF] text-black px-3 py-2 rounded-sm hover:bg-[#33EFFF] transition-colors"
          >
            <Plus size={14} strokeWidth={2} /> New
          </Link>
        }
      />

      <div className="p-8 space-y-6">
        <div className="flex flex-col md:flex-row gap-3 md:items-center">
          <div className="relative flex-1">
            <Search size={14} strokeWidth={1.5} className="absolute left-3 top-1/2 -translate-y-1/2 text-[var(--ff-text-muted)]" />
            <input
              data-testid="projects-search"
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="Search by name, niche, topic…"
              className="w-full bg-[var(--ff-surface)] border border-[var(--ff-border)] pl-9 pr-3 py-2.5 text-sm rounded-sm focus:border-[#00E5FF]"
            />
          </div>
          <select
            data-testid="projects-status-filter"
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
            className="bg-[var(--ff-surface)] border border-[var(--ff-border)] px-3 py-2.5 text-sm rounded-sm focus:border-[#00E5FF] font-mono uppercase tracking-wider"
          >
            <option value="all">All statuses</option>
            {Object.keys(STATUS_META).map((s) => (
              <option key={s} value={s}>{STATUS_META[s].label}</option>
            ))}
          </select>
        </div>

        {loading ? (
          <div className="text-sm text-[var(--ff-text-muted)] font-mono">Loading projects…</div>
        ) : filtered.length === 0 ? (
          <div className="border border-[var(--ff-border)] border-dashed p-10 text-center rounded-sm">
            <p className="text-sm text-[var(--ff-text-muted)] mb-4">No projects match your filter.</p>
            <Link
              to="/app/projects/new"
              className="inline-flex items-center gap-2 bg-[#00E5FF] text-black px-4 py-2 text-sm font-semibold rounded-sm"
            >
              <Plus size={14} strokeWidth={2} /> Create new project
            </Link>
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
            {filtered.map((p, i) => (
              <ProjectCard key={p.id} project={p} index={i} />
            ))}
          </div>
        )}
      </div>
    </AppShell>
  );
}
