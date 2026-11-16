import { api } from "./api";

export const ACTIVE_RENDER_STATES = new Set(["queued", "validating", "preparing_assets", "rendering", "verifying"]);

export const normalizeRenderJob = (job) => ({
  ...job,
  status: String(job.status || "").toLowerCase(),
});

export async function listRenderJobs(projectId, timeout = 15000) {
  const { data } = await api.get(`/projects/${projectId}/render/jobs`, { timeout });
  if (!Array.isArray(data)) throw new Error("Render history response was not a list.");
  return data.map(normalizeRenderJob);
}

export function shouldReconnectRender(error) {
  const status = error.response?.status;
  // 409 is a real refusal (settings changed, lease busy, revision mismatch) as
  // often as it is a lost start. It is resolved with one status read, not the
  // gateway retry loop.
  return !error.response || [408, 502, 503, 504, 520, 522, 523, 524].includes(status);
}

function submittedJob(jobs, previousJobs) {
  const previousIds = new Set((previousJobs || []).map((job) => job.id));
  return jobs.find((item) => ACTIVE_RENDER_STATES.has(item.status))
    || jobs.find((item) => !previousIds.has(item.id))
    || null;
}

export async function reconnectRender(projectId, previousJobs) {
  for (let attempt = 0; attempt < 5; attempt += 1) {
    if (attempt) await new Promise((resolve) => setTimeout(resolve, 2500));
    try {
      const jobs = await listRenderJobs(projectId, 5000);
      const job = submittedJob(jobs, previousJobs);
      if (job) return { job, jobs };
    } catch {
      // The gateway can briefly drop status reads as well as the start response.
    }
  }
  return null;
}

export async function checkRenderConflict(projectId, previousJobs) {
  const jobs = await listRenderJobs(projectId, 5000);
  return { job: submittedJob(jobs, previousJobs), jobs };
}
