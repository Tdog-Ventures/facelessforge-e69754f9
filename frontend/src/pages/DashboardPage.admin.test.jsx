import React, { act } from "react";
import { createRoot } from "react-dom/client";
import DashboardPage from "./DashboardPage";

jest.mock("../components/AppShell", () => ({ children }) => <div>{children}</div>);
jest.mock("../components/TopBar", () => ({ subtitle, right }) => (
  <header>
    <span data-testid="subtitle">{subtitle}</span>
    {right}
  </header>
));
jest.mock("../components/BillingBar", () => () => null);
jest.mock("../components/ProjectCard", () => () => null);
jest.mock("../lib/api", () => ({
  api: { get: jest.fn() },
}));
jest.mock("recharts", () => ({
  ResponsiveContainer: ({ children }) => <div>{children}</div>,
  LineChart: () => null,
  Line: () => null,
  XAxis: () => null,
  YAxis: () => null,
  Tooltip: () => null,
  CartesianGrid: () => null,
}));
jest.mock("react-router-dom", () => ({
  Link: ({ children, ...props }) => <a {...props}>{children}</a>,
}), { virtual: true });

const { api } = require("../lib/api");

beforeEach(() => {
  global.IS_REACT_ACT_ENVIRONMENT = true;
  api.get.mockImplementation(async (path) => {
    if (String(path).includes("analytics")) {
      return { data: { is_admin_view: true, projects_over_time: [], status_counts: {}, total_projects: 0 } };
    }
    return { data: [] };
  });
});

test("admin view stays in the subtitle and is not a button", async () => {
  const element = document.createElement("div");
  document.body.appendChild(element);
  const root = createRoot(element);
  await act(async () => { root.render(<DashboardPage />); });
  expect(element.querySelector("[data-testid='subtitle']").textContent).toContain("ADMIN VIEW");
  const buttons = [...element.querySelectorAll("button, a")].map((node) => node.textContent);
  expect(buttons.some((text) => text.includes("ADMIN VIEW"))).toBe(false);
  expect(element.innerHTML).not.toContain("bg-amber-500");
  await act(async () => { root.unmount(); });
  element.remove();
});
