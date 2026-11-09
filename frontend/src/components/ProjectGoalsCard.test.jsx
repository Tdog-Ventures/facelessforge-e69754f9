import React, { act } from "react";
import { createRoot } from "react-dom/client";
import ProjectGoalsCard from "./ProjectGoalsCard";

jest.mock("../lib/api", () => ({
  api: { patch: jest.fn() },
  formatApiError: (detail) => String(detail || ""),
}));
jest.mock("sonner", () => ({ toast: { success: jest.fn(), error: jest.fn() } }));

const filled = {
  audience: "Home cooks",
  monetisation_intent: "Educate audience",
  cta_goal: "Subscribe",
};

async function render(project) {
  const element = document.createElement("div");
  document.body.appendChild(element);
  const root = createRoot(element);
  await act(async () => {
    root.render(<ProjectGoalsCard projectId="p1" project={project} canEdit onChange={() => {}} />);
  });
  return {
    element,
    async unmount() {
      await act(async () => { root.unmount(); });
      element.remove();
    },
  };
}

beforeEach(() => { global.IS_REACT_ACT_ENVIRONMENT = true; });

test("a complete project keeps the goals form collapsed", async () => {
  const view = await render(filled);
  expect(view.element.querySelector("[data-testid='project-goals-summary']").textContent)
    .toContain("Home cooks");
  expect(view.element.querySelector("[data-testid='field-audience-human']")).toBeNull();
  await view.unmount();
});

test("a missing answer opens the form without pretending a default was saved", async () => {
  const view = await render({ audience: "", monetisation_intent: "", cta_goal: "" });
  const goal = view.element.querySelector("[data-testid='field-goal-human']");
  const action = view.element.querySelector("[data-testid='field-cta-human']");
  expect(view.element.querySelector("[data-testid='field-audience-human']").value).toBe("");
  expect(goal.value).toBe("");
  expect(action.value).toBe("");
  expect(goal.textContent).not.toMatch(/^Grow channel/);
  await view.unmount();
});
