import React, { act } from "react";
import { createRoot } from "react-dom/client";
import ProjectTabBar, { CORE_TABS, RECORD_TABS } from "./ProjectTabBar";

beforeEach(() => {
  global.IS_REACT_ACT_ENVIRONMENT = true;
  window.HTMLElement.prototype.hasPointerCapture = () => false;
  window.HTMLElement.prototype.releasePointerCapture = () => {};
  window.HTMLElement.prototype.scrollIntoView = () => {};
});

async function render(tab = "scenes") {
  const element = document.createElement("div");
  document.body.appendChild(element);
  const root = createRoot(element);
  const onChange = jest.fn();
  await act(async () => { root.render(<ProjectTabBar tab={tab} onChange={onChange} />); });
  return {
    element,
    onChange,
    async unmount() {
      await act(async () => { root.unmount(); });
      element.remove();
    },
  };
}

test("the main row keeps the video tabs and hides records behind one menu", async () => {
  const view = await render();
  const row = view.element.querySelector("[data-testid='project-tab-row']");
  for (const item of CORE_TABS) {
    expect(row.querySelector(`[data-testid='tab-${item.id}']`)).not.toBeNull();
  }
  for (const item of RECORD_TABS) {
    expect(row.textContent).not.toContain(item.label);
  }
  expect(view.element.querySelector("[data-testid='project-tab-fade']")).not.toBeNull();
  expect(row.className).toContain("ff-tab-scroll");
  const records = view.element.querySelector("[data-testid='tab-records']");
  expect(records.tagName).toBe("BUTTON");
  await act(async () => { records.click(); });
  const intake = document.body.querySelector("[data-testid='tab-intake']");
  expect(intake).not.toBeNull();
  expect(intake.getAttribute("role")).toBe("menuitem");
  await act(async () => { intake.click(); });
  expect(view.onChange).toHaveBeenCalledWith("intake");
  await view.unmount();
});

test("an open record names itself on the Records button", async () => {
  const view = await render("rights");
  expect(view.element.querySelector("[data-testid='tab-records']").textContent).toContain("Rights");
  await view.unmount();
});
