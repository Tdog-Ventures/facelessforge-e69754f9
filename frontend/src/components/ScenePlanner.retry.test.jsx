import React, { act } from "react";
import { createRoot } from "react-dom/client";
import ScenePlanner from "./ScenePlanner";
import { api } from "../lib/api";
import { pendingAssetAttachment } from "../lib/assetAttachmentOperation";

const mockConfirm = jest.fn();
jest.mock("./ConfirmDialog", () => ({ useConfirm: () => mockConfirm }));
jest.mock("./BillingBar", () => () => null);
jest.mock("./StockAssetModal", () => () => null);
// Router is outside this component contract; Jest 27 cannot resolve its v7 exports.
jest.mock("react-router-dom", () => ({ useNavigate: () => jest.fn() }), { virtual: true });
jest.mock("../lib/api", () => ({
  api: { get: jest.fn(), post: jest.fn() }, API: "/api",
  formatApiError: (detail) => String(detail || ""),
}));
jest.mock("sonner", () => ({ toast: {
  loading: jest.fn(() => "toast"), success: jest.fn(), info: jest.fn(), error: jest.fn(),
} }));

const project = { id: "p1", production_mode: "editorial", target_duration: 30 };
const props = {
  projectId: "p1", project, canEdit: true, hasScript: true, onChange: jest.fn(),
  scenes: [{ id: "s1", scene_number: 1, narration_text: "Approved narration", end_time: 27.5 }],
  attachedAssets: [{ id: "original", scene_id: "s1", asset_type: "stock_video" }],
};
const complete = { total: 1, attached: 4, skipped: 0, failed: 0, aborted: null };
let element, root, balance;

async function mount() {
  element = document.createElement("div");
  document.body.appendChild(element);
  root = createRoot(element);
  await act(async () => { root.render(<ScenePlanner {...props} />); });
}

async function unmount() {
  if (root) await act(async () => { root.unmount(); });
  root = null;
  element?.remove();
}

async function click() {
  const button = element.querySelector('[data-testid="auto-attach-btn"]');
  expect(button.disabled).toBe(false);
  await act(async () => { button.click(); });
}

beforeEach(() => {
  global.IS_REACT_ACT_ENVIRONMENT = true;
  window.sessionStorage.clear();
  jest.clearAllMocks();
  balance = 4;
  mockConfirm.mockResolvedValue(true);
  api.get.mockImplementation(async (path) => ({ data: path === "/billing/credits"
    ? { remaining: balance } : { project, assets: [{ id: "paid-selection" }] } }));
});

afterEach(async () => { await unmount(); jest.restoreAllMocks(); });

test("lost response and remount retry preserve the key, paid selection and zero balance", async () => {
  const committed = new Map();
  let charges = 0;
  let selectedIds = ["original"];
  api.post.mockImplementation(async (_path, payload) => {
    if (committed.has(payload.idempotency_key)) return { data: { ...complete, replayed: true } };
    selectedIds = ["paid-1", "paid-2", "paid-3", "paid-4"];
    balance -= 4;
    charges += 4;
    committed.set(payload.idempotency_key, selectedIds.slice());
    throw new Error("Response lost after server commit");
  });
  await mount();
  await click();
  const firstPayload = { ...api.post.mock.calls[0][1] };
  expect(pendingAssetAttachment("p1").payload).toEqual(firstPayload);
  const paidIds = selectedIds.slice();
  await unmount();
  await mount(); // persisted operation survives refresh; authoritative balance is now zero
  expect(element.querySelector('[data-testid="auto-attach-btn"]').textContent).toContain("Retry asset update");
  await click();
  expect(api.post.mock.calls[1][1]).toEqual(firstPayload);
  expect(mockConfirm).toHaveBeenCalledTimes(1);
  expect(selectedIds).toEqual(paidIds);
  expect(charges).toBe(4);
  expect(balance).toBe(0);
  expect(pendingAssetAttachment("p1")).toBeNull();
});

test("a resumable failed 2xx outcome keeps identity until successful retry", async () => {
  api.post.mockResolvedValueOnce({ data: { ...complete, failed: 1 } })
    .mockResolvedValueOnce({ data: complete });
  await mount();
  await click();
  const first = api.post.mock.calls[0][1];
  expect(pendingAssetAttachment("p1").payload).toEqual(first);
  await click();
  expect(api.post.mock.calls[1][1]).toEqual(first);
  expect(pendingAssetAttachment("p1")).toBeNull();
});

test("unavailable retry storage prevents a chargeable request", async () => {
  jest.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("Storage unavailable"); });
  await mount();
  await click();
  expect(api.post).not.toHaveBeenCalled();
});
