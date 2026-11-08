import React, { act } from "react";
import { createRoot } from "react-dom/client";
import RenderPanel from "./RenderPanel";
import { api } from "../lib/api";

jest.mock("../lib/api", () => ({
  api: { get: jest.fn(), post: jest.fn() },
  API: "/api",
  formatApiError: (detail) => (typeof detail === "string" ? detail : ""),
}));
jest.mock("sonner", () => ({ toast: { error: jest.fn(), success: jest.fn(), warning: jest.fn() } }));
jest.mock("./MusicPanel", () => () => null);
jest.mock("./voiceover/VoiceStylePicker", () => () => null);

let element;
let root;

async function mount() {
  element = document.createElement("div");
  document.body.appendChild(element);
  root = createRoot(element);
  await act(async () => {
    root.render(
      <RenderPanel
        projectId="p1"
        project={{ id: "p1", target_duration: 136, aspect_ratio: "16:9" }}
        script={{ full_script: "hello" }}
        scenes={[]}
        metadata={{}}
        assets={[]}
        canEdit
        onChange={() => {}}
      />,
    );
  });
}

beforeEach(() => {
  global.IS_REACT_ACT_ENVIRONMENT = true;
  jest.clearAllMocks();
});

afterEach(async () => {
  if (root) await act(async () => { root.unmount(); });
  element?.remove();
});

test("a failed preflight leaves an error and a retry, not a permanent loading line", async () => {
  api.get.mockImplementation(async (path) => {
    if (String(path).includes("/render/preflight")) {
      const error = new Error("timeout");
      error.response = { status: 500, data: { detail: "preflight failed" } };
      throw error;
    }
    return { data: [] };
  });
  await mount();
  expect(element.textContent).not.toContain("Loading render state");
  expect(element.querySelector("[data-testid='render-load-error']").textContent).toContain("HTTP 500");
  expect(element.querySelector("[data-testid='render-retry']")).toBeTruthy();
});

test("preflight still renders when render history fails", async () => {
  api.get.mockImplementation(async (path) => {
    if (String(path).includes("/render/jobs")) {
      const error = new Error("history down");
      error.response = { status: 503, data: { detail: "history unavailable" } };
      throw error;
    }
    return { data: { ok: true, issues: [], checklist: [] } };
  });
  await mount();
  expect(element.textContent).toContain("Start render");
  expect(element.querySelector("[data-testid='render-jobs-warning']").textContent).toContain("HTTP 503");
});
