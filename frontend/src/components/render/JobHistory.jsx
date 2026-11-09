import React from "react";

export default function JobHistory({ jobs, projectId }) {
  if (!jobs || jobs.length === 0) return null;
  return (
    <div className="border border-[var(--ff-border)] bg-[var(--ff-surface)] rounded-sm overflow-hidden">
      <div className="px-4 py-3 border-b border-[var(--ff-border)] font-mono text-[10px] uppercase tracking-widest text-[var(--ff-text-muted)]">
        Render history · {jobs.length} jobs
      </div>
      <div className="divide-y divide-zinc-800">
        {jobs.slice(0, 8).map((j) => {
          const status = String(j.status || "").toLowerCase();
          return (
          <div
            key={j.id}
            data-testid={`render-history-${j.id}`}
            className="px-4 py-3 flex items-center gap-3 text-sm"
          >
            <span className="font-mono text-[10px] uppercase tracking-widest"
                  style={{
                    color: status === "completed" ? "#00FF66"
                      : status === "failed" ? "#FF3366"
                      : status === "cancelled" ? "#71717A" : "#00E5FF",
                  }}>
              {status}
            </span>
            <span className="font-mono text-[10px] text-[var(--ff-text-muted)]">
              {new Date(j.created_at).toLocaleString('en-AU', {timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone, hour12:true})}
            </span>
            <span className="flex-1" />
            {status === "completed" && (j.output_storage_key || j.output_url) && (
              <a
                href={`/api/projects/${projectId}/render/jobs/${j.id}/download`}
                target="_blank"
                rel="noreferrer"
                className="font-mono text-[10px] uppercase tracking-widest text-[#00E5FF] hover:underline"
              >
                Open
              </a>
            )}
          </div>
          );
        })}
      </div>
    </div>
  );
}
