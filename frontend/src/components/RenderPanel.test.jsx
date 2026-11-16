import React, { act } from "react";
import { createRoot } from "react-dom/client";
import RenderPanel from "./RenderPanel";
import { api } from "../lib/api";
import { toast } from "sonner";

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
        project={{ id: "p1", target_duration: 136, aspect_ratio: "16:9", selected_thumbnail_asset_id: "thumb" }}
        script={{ full_script: "hello" }}
        scenes={[]}
        metadata={{}}
        assets={[{ id: "thumb", asset_type: "generated_thumbnail", preview_url: "/wide-thumbnail.png" }]}
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

test("preflight still renders when render history fails, and start stays disabled", async () => {
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
  expect(element.querySelector("[data-testid='render-start-btn']").disabled).toBe(true);
  expect(element.querySelector("[data-testid='render-jobs-warning']").textContent).toContain("HTTP 503");
});

test("an empty preflight object is an error, not a crash", async () => {
  api.get.mockImplementation(async (path) => {
    if (String(path).includes("/render/preflight")) return { data: {} };
    return { data: [] };
  });
  await mount();
  expect(element.querySelector("[data-testid='render-load-error']").textContent).toContain("unusable");
});

test("a later history failure keeps the running job", async () => {
  let jobsCalls = 0;
  api.get.mockImplementation(async (path) => {
    if (String(path).includes("/render/jobs/") && !String(path).endsWith("/render/jobs")) {
      return { data: { id: "job-1", status: "rendering", progress: 40 } };
    }
    if (String(path).includes("/render/jobs")) {
      jobsCalls += 1;
      if (jobsCalls > 1) {
        const error = new Error("history");
        error.response = { status: 503, data: { detail: "history unavailable" } };
        throw error;
      }
      return { data: [{ id: "job-1", status: "rendering", progress: 40 }] };
    }
    return { data: { ok: false, issues: ["Voiceover"], checklist: [{ key: "voiceover", label: "Voiceover", ok: false, hint: "" }] } };
  });
  await mount();
  expect(element.textContent).toContain("Render running");
  await act(async () => { element.querySelector("[data-testid='render-refresh']").click(); });
  expect(element.textContent).toContain("Render running");
  expect(element.querySelector("[data-testid='render-start-btn']").disabled).toBe(true);
});

function mockReady(credits) {
  api.get.mockImplementation(async (path) => {
    const value = String(path);
    if (value.includes("/billing/credits")) return { data: credits };
    if (value.includes("/render/jobs")) return { data: [] };
    return { data: { ok: true, issues: [], checklist: [] } };
  });
}

test("start stays off when the balance is under 3 credits", async () => {
  mockReady({ remaining: 2, credits: 2, operator_unmetered: false });
  api.post.mockResolvedValue({ data: { id: "job", status: "queued" } });
  await mount();
  const button = element.querySelector("[data-testid='render-start-btn']");
  expect(button.disabled).toBe(true);
  expect(element.querySelector("[data-testid='render-credit-stop']").textContent)
    .toContain("3 credits required, 2 remaining");
  await act(async () => { button.click(); });
  expect(api.post).not.toHaveBeenCalled();
});

test("start runs when the balance covers the 3-credit render", async () => {
  mockReady({ remaining: 3, credits: 3, operator_unmetered: false });
  api.post.mockResolvedValue({ data: { id: "job-9", status: "queued" } });
  await mount();
  const button = element.querySelector("[data-testid='render-start-btn']");
  expect(button.disabled).toBe(false);
  await act(async () => { button.click(); });
  expect(api.post).toHaveBeenCalledWith("/projects/p1/render/start", expect.any(Object));
});

test("an unmetered balance can render with no credits left", async () => {
  mockReady({ remaining: 0, credits: 0, operator_unmetered: true });
  await mount();
  expect(element.querySelector("[data-testid='render-start-btn']").disabled).toBe(false);
  expect(element.querySelector("[data-testid='render-credit-stop']")).toBeNull();
});

test("a footage shortfall is shown on the pre-render panel and blocks start", async () => {
  const sentence = "Scene 2 has 1 of 8 required distinct clips - run Find footage for scene 2.";
  api.get.mockImplementation(async (path) => {
    const value = String(path);
    if (value.includes("/billing/credits")) return { data: { remaining: 9, credits: 9, operator_unmetered: false } };
    if (value.includes("/render/jobs")) return { data: [] };
    return {
      data: {
        ok: false,
        issues: ["Scene visuals meet 3-second pacing", sentence],
        checklist: [{
          key: "scene_assets",
          label: "Scene visuals meet 3-second pacing",
          ok: false,
          hint: sentence,
        }],
      },
    };
  });
  await mount();
  const gate = element.querySelector("[data-testid='footage-gate']");
  expect(gate.textContent).toContain("Scene 2 has 1 of 8 required distinct clips");
  expect(gate.textContent).toContain("Find footage for scene 2");
  expect(element.querySelector("[data-testid='render-start-btn']").disabled).toBe(true);
  expect(element.querySelector("[data-testid='pre-scene_assets']").textContent).toContain(sentence);
});

test("a missing credit balance keeps the render button off", async () => {
  api.get.mockImplementation(async (path) => {
    if (String(path).includes("/billing/credits")) throw new Error("balance down");
    if (String(path).includes("/render/jobs")) return { data: [] };
    return { data: { ok: true, issues: [], checklist: [] } };
  });
  await mount();
  const button = element.querySelector("[data-testid='render-start-btn']");
  expect(button.disabled).toBe(true);
  expect(element.querySelector("[data-testid='render-credit-stop']").textContent)
    .toContain("Render stays off");
  await act(async () => { button.click(); });
  expect(api.post).not.toHaveBeenCalled();
});

test("a 524 reconnects to the queued job without sending a second start request", async () => {
  const previous = { id: "old-job", status: "completed" };
  let jobsReads = 0;
  api.get.mockImplementation(async (path) => {
    if (path === "/billing/credits") return { data: { remaining: 3 } };
    if (path.endsWith("/render/jobs")) {
      return { data: ++jobsReads === 1 ? [previous] : [
        { id: "new-job", status: "rendering", progress: 42, current_step: "rendering" },
        previous,
      ] };
    }
    return { data: { ok: true, issues: [], checklist: [] } };
  });
  api.post.mockRejectedValue({ response: { status: 524 } });

  await mount();
  await act(async () => { element.querySelector("[data-testid='render-start-btn']").click(); });

  expect(api.post).toHaveBeenCalledTimes(1);
  expect(element.querySelector("[data-testid='render-job-rendering']").textContent).toContain("42%");
  expect(element.querySelector("[data-testid='render-start-btn']").disabled).toBe(true);
});

const REFUSAL = "Project settings changed while the render was being prepared; re-check the project and retry.";

function jobReads() {
  return api.get.mock.calls.filter(([path]) => String(path).endsWith("/render/jobs"));
}

test("a 409 with an active job resumes that job", async () => {
  let jobsReads = 0;
  api.get.mockImplementation(async (path) => {
    if (path === "/billing/credits") return { data: { remaining: 3 } };
    if (String(path).endsWith("/render/jobs")) {
      jobsReads += 1;
      return { data: jobsReads === 1 ? [] : [{ id: "live", status: "rendering", progress: 18 }] };
    }
    return { data: { ok: true, issues: [], checklist: [] } };
  });
  api.post.mockRejectedValue({ response: { status: 409, data: { detail: "A render is already in progress for this project." } } });

  await mount();
  await act(async () => { element.querySelector("[data-testid='render-start-btn']").click(); });

  expect(api.post).toHaveBeenCalledTimes(1);
  expect(jobReads()).toHaveLength(2);
  expect(element.querySelector("[data-testid='render-job-rendering']").textContent).toContain("18%");
  expect(element.querySelector("[data-testid='render-start-btn']").disabled).toBe(true);
  expect(element.querySelector("[data-testid='render-start-error']")).toBeNull();
  expect(element.textContent).not.toContain("Render start is unconfirmed");
});

test("a 409 without a job shows the server message and leaves Start enabled", async () => {
  api.get.mockImplementation(async (path) => {
    if (path === "/billing/credits") return { data: { remaining: 3 } };
    if (String(path).endsWith("/render/jobs")) return { data: [] };
    return { data: { ok: true, issues: [], checklist: [] } };
  });
  api.post.mockRejectedValue({ response: { status: 409, data: { detail: REFUSAL } } });

  await mount();
  const button = element.querySelector("[data-testid='render-start-btn']");
  expect(button.disabled).toBe(false);
  await act(async () => { button.click(); });

  expect(api.post).toHaveBeenCalledTimes(1);
  expect(jobReads()).toHaveLength(2);
  expect(toast.error).toHaveBeenCalledWith(REFUSAL);
  expect(element.querySelector("[data-testid='render-start-error']").textContent).toBe(REFUSAL);
  expect(element.querySelector("[data-testid='render-start-btn']").disabled).toBe(false);
  expect(element.textContent).not.toContain("Render start is unconfirmed");
});

test("a portrait render uses a narrow player and omits the landscape poster", async () => {
  api.get.mockImplementation(async (path) => {
    if (path === "/billing/credits") return { data: { remaining: 3 } };
    if (path.endsWith("/render/jobs")) {
      return { data: [{ id: "portrait", status: "completed", aspect_ratio: "9:16" }] };
    }
    return { data: { ok: true, issues: [], checklist: [] } };
  });

  await mount();
  const video = element.querySelector("[data-testid='render-video-player']");
  expect(video.style.aspectRatio).toBe("9 / 16");
  expect(video.style.maxWidth).toBe("39.375vh");
  expect(video.getAttribute("poster")).toBeNull();
});

test("a 400 footage refusal shows the named scene and does not reconnect", async () => {
  const sentence = "Scene 2 has 1 of 8 required distinct clips - run Find footage for scene 2.";
  api.get.mockImplementation(async (path) => {
    if (path === "/billing/credits") return { data: { remaining: 3 } };
    if (String(path).endsWith("/render/jobs")) return { data: [] };
    return { data: { ok: true, issues: [], checklist: [] } };
  });
  api.post.mockRejectedValue({
    response: {
      status: 400,
      data: {
        detail: {
          message: sentence,
          issues: [sentence],
          checklist: [{
            key: "scene_assets",
            label: "Scene visuals meet 3-second pacing",
            ok: false,
            hint: sentence,
          }],
        },
      },
    },
  });

  await mount();
  await act(async () => { element.querySelector("[data-testid='render-start-btn']").click(); });

  expect(api.post).toHaveBeenCalledTimes(1);
  expect(jobReads()).toHaveLength(1);
  expect(toast.error).toHaveBeenCalledWith("Not enough footage", expect.objectContaining({ description: sentence }));
  expect(element.querySelector("[data-testid='footage-gate']").textContent).toContain(sentence);
  expect(element.querySelector("[data-testid='render-start-btn']").disabled).toBe(true);
  expect(element.textContent).not.toContain("Render start is unconfirmed");
  expect(element.textContent).not.toContain("Reconnecting");
});
