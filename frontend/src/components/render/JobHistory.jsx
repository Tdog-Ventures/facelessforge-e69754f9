import React from "react";

export default function JobHistory({ jobs }) {
  if (!jobs || jobs.length === 0) return null;
  return (
    <div className="border border-zinc-800 bg-[#121212] rounded-sm overflow-hidden">
      <div className="px-4 py-3 border-b border-zinc-800 font-mono text-[10px] uppercase tracking-widest text-zinc-500">
        Render history · {jobs.length} jobs
      </div>
      <div className="divide-y divide-zinc-800">
        {jobs.slice(0, 8).map((j) => (
          <div
            key={j.id}
            data-testid={`render-history-${j.id}`}
            className="px-4 py-3 flex items-center gap-3 text-sm"
          >
            <span className="font-mono text-[10px] uppercase tracking-widest"
                  style={{
                    color: j.status === "completed" ? "#00FF66"
                      : j.status === "failed" ? "#FF3366"
                      : j.status === "cancelled" ? "#71717A" : "#00E5FF",
                  }}>
              {j.status}
            </span>
            <span className="font-mono text-[10px] text-zinc-500">
              {new Date(j.created_at).toLocaleString()}
            </span>
            <span className="flex-1" />
            {j.output_url && (
              <a
                href={j.output_url}
                target="_blank"
                rel="noreferrer"
                className="font-mono text-[10px] uppercase tracking-widest text-[#00E5FF] hover:underline"
              >
                Open
              </a>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
