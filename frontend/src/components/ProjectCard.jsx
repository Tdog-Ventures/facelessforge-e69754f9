import React from "react";
import { Link } from "react-router-dom";
import StatusBadge from "./StatusBadge";
import { formatCurrency, formatDuration, relativeTime, qualityColor } from "../lib/format";
import { Clock, DollarSign } from "lucide-react";

export default function ProjectCard({ project, index = 0 }) {
  const color = qualityColor(project.quality_score || 0);
  return (
    <Link
      to={`/app/projects/${project.id}`}
      data-testid={`project-card-${project.id}`}
      className={`ff-card-hover ff-rise ${index < 4 ? `ff-rise-${index + 1}` : ""} block border border-[var(--ff-border)] bg-[var(--ff-surface)] p-5 rounded-sm`}
    >
      <div className="flex items-start justify-between gap-4 mb-4">
        <div className="min-w-0">
          <div className="font-mono text-[10px] text-[var(--ff-text-dim)] uppercase tracking-widest mb-2">
            {project.niche}
          </div>
          <h3 className="text-base font-semibold text-[var(--ff-text)] truncate">{project.name}</h3>
        </div>
        <StatusBadge status={project.status} />
      </div>

      <p className="text-sm text-[var(--ff-text-muted)] line-clamp-2 mb-5 leading-relaxed min-h-[2.5rem]">
        {project.topic}
      </p>

      <div className="flex items-center justify-between pt-4 border-t border-[var(--ff-border)]">
        <div className="flex items-center gap-3 text-[var(--ff-text-dim)] font-mono text-[11px]">
          <span className="flex items-center gap-1.5">
            <Clock size={12} strokeWidth={1.5} />
            {formatDuration(project.target_duration)}
          </span>
          <span className="flex items-center gap-1.5">
            <DollarSign size={12} strokeWidth={1.5} />
            {formatCurrency(project.estimated_cost)}
          </span>
          <span>{relativeTime(project.created_at)}</span>
        </div>
        <div className="flex items-center gap-2">
          <span className="font-mono text-sm tabular-nums" style={{ color }}>
            {project.quality_score || 0}
          </span>
          <div className="ff-progress w-16">
            <span style={{ width: `${project.quality_score || 0}%`, background: color }} />
          </div>
        </div>
      </div>
    </Link>
  );
}
