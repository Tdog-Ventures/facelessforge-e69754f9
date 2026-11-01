// A pending paid action survives a lost response, a refresh, and partial failure.
// Do not expire it: only a completed operation plus refreshed UI may clear it.
const storageKey = (projectId) => `ff:auto-attach:${projectId}`;

export function pendingAssetAttachment(projectId) {
  const raw = window.sessionStorage.getItem(storageKey(projectId));
  if (!raw) return null;
  const operation = JSON.parse(raw);
  if (operation.projectId !== projectId ||
      typeof operation.payload?.replace_existing !== "boolean" ||
      operation.payload.media_type !== "both" ||
      !/^[A-Za-z0-9._:-]{8,80}$/.test(operation.payload.idempotency_key || "")) {
    throw new Error("The pending asset action needs reconciliation before starting another one.");
  }
  return operation;
}

export function beginAssetAttachment(projectId, replaceExisting) {
  const pending = pendingAssetAttachment(projectId);
  if (pending) return pending;
  const operation = {
    projectId,
    payload: {
      replace_existing: replaceExisting,
      media_type: "both",
      idempotency_key: `aa-${globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`}`,
    },
  };
  // If storage is unavailable, fail BEFORE submitting a chargeable action.
  window.sessionStorage.setItem(storageKey(projectId), JSON.stringify(operation));
  return operation;
}

export function finishAssetAttachment(operation, summary) {
  if (!Number.isInteger(summary?.total) || summary.failed !== 0 || summary.aborted) return false;
  const pending = pendingAssetAttachment(operation.projectId);
  if (pending?.payload.idempotency_key !== operation.payload.idempotency_key) return false;
  window.sessionStorage.removeItem(storageKey(operation.projectId));
  return true;
}
