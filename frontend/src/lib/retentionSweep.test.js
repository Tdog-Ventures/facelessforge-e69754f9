import { confirmRetentionSweep, retentionConfirmCopy } from "./retentionSweep";

const preview = {
  retention_days: 7,
  renders_removed: 2,
  render_workdirs_removed: 1,
  orphan_project_dirs_removed: 3,
  remote_objects_removed: 1,
  stale_jobs_marked_failed: 4,
};

test("the confirm text names the real cutoffs and says deletion is permanent", () => {
  const text = retentionConfirmCopy(preview);
  expect(text).toContain("older than 7 days");
  expect(text).toContain("older than 1 hour");
  expect(text).toContain("more than 1 hour");
  expect(text).toContain("marked failed");
  expect(text).toContain("cannot be restored");
  expect(text).toContain("2 files");
});

test("cancel after the preview does not run the sweep", async () => {
  const api = {
    get: jest.fn(async () => ({ data: preview })),
    post: jest.fn(),
  };
  const confirm = jest.fn(async () => false);
  await expect(confirmRetentionSweep(api, confirm)).resolves.toBeNull();
  expect(api.get).toHaveBeenCalledWith("/admin/retention/preview");
  expect(api.post).not.toHaveBeenCalled();
  expect(confirm.mock.calls[0][0].description).toContain("cannot be restored");
});
