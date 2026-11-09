import React, { act } from "react";
import { createRoot } from "react-dom/client";
import ThumbnailPanel from "./ThumbnailPanel";
import { api } from "../lib/api";

jest.mock("../lib/api", () => ({
  api: { get: jest.fn(), post: jest.fn() },
  formatApiError: (detail) => (typeof detail === "string" ? detail : detail?.message || ""),
}));
jest.mock("./ConfirmDialog", () => ({ useConfirm: () => jest.fn() }));
jest.mock("sonner", () => ({ toast: {
  loading: jest.fn(() => "toast"), success: jest.fn(), error: jest.fn(),
} }));

const briefs = [
  { id: "concept-1", asset_type: "thumbnail_concept", brief: { thumbnail_title_text: "Poach it" } },
  { id: "concept-2", asset_type: "thumbnail_concept", brief: { thumbnail_title_text: "Fry it" } },
];

async function renderPanel() {
  const element = document.createElement("div");
  document.body.appendChild(element);
  const root = createRoot(element);
  await act(async () => {
    root.render(
      <ThumbnailPanel
        projectId="p1"
        assets={briefs}
        selectedThumbnailId={null}
        canEdit
        onChange={jest.fn()}
      />,
    );
  });
  return {
    element,
    async unmount() {
      await act(async () => { root.unmount(); });
      element.remove();
    },
  };
}

beforeEach(() => {
  global.IS_REACT_ACT_ENVIRONMENT = true;
  api.get.mockResolvedValue({ data: { mock: true, configured: true, provider: "mock" } });
  api.post.mockResolvedValue({ data: { assets: briefs } });
});

test("opening the tab does not generate an image unless the checkbox is on", async () => {
  const view = await renderPanel();
  const box = view.element.querySelector("input[type='checkbox']");
  expect(box.checked).toBe(false);
  expect(box.parentElement.textContent).toContain("Create an image");
  expect(api.post).not.toHaveBeenCalled();
  await view.unmount();
});

test("the count picker sends 1, 2, or 3 for that concept", async () => {
  const view = await renderPanel();
  const count = view.element.querySelector("[data-testid='generate-count-concept-2']");
  await act(async () => {
    count.value = "2";
    count.dispatchEvent(new Event("change", { bubbles: true }));
  });
  const button = view.element.querySelector("[data-testid='generate-variants-btn-concept-2']");
  await act(async () => { button.click(); });
  expect(api.post).toHaveBeenCalledWith(
    "/projects/p1/thumbnails/concept-2/generate",
    { variants: 2 },
  );
  const single = view.element.querySelector("[data-testid='generate-image-btn-concept-2']");
  await act(async () => { single.click(); });
  expect(api.post).toHaveBeenLastCalledWith(
    "/projects/p1/thumbnails/concept-2/generate",
    { variants: 1 },
  );
  await view.unmount();
});
