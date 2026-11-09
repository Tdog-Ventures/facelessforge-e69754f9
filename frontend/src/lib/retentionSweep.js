export function retentionConfirmCopy(preview) {
  const days = Number(preview?.retention_days);
  const dayLabel = Number.isFinite(days) ? `${days} day${days === 1 ? "" : "s"}` : "the configured retention window";
  return [
    `Permanently deletes finished videos older than ${dayLabel} (${preview?.renders_removed ?? 0} files), scratch folders older than 1 hour (${preview?.render_workdirs_removed ?? 0}), and leftover files for projects that are already gone (${preview?.orphan_project_dirs_removed ?? 0} folders).`,
    `Cloud copies older than ${dayLabel}: ${preview?.remote_objects_removed ?? 0}.`,
    `Renders that have been queued or running for more than 1 hour will be marked failed (${preview?.stale_jobs_marked_failed ?? 0}).`,
    "Deleted files cannot be restored.",
  ].join(" ");
}

export async function confirmRetentionSweep(api, confirm) {
  const { data } = await api.get("/admin/retention/preview");
  const ok = await confirm({
    title: "Run retention sweep now?",
    description: retentionConfirmCopy(data),
    confirmLabel: "Run cleanup",
    tone: "destructive",
  });
  if (!ok) return null;
  const { data: report } = await api.post("/admin/retention/run");
  return report;
}
