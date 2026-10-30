import "@testing-library/jest-dom";
import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";
import { ThemeProvider } from "next-themes";
import TopBar from "./TopBar";

// jsdom doesn't implement matchMedia — mock it before importing components
Object.defineProperty(window, "matchMedia", {
  writable: true,
  value: (query) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  }),
});

// Mock lucide icons to keep tests simple
jest.mock("lucide-react", () => ({
  Activity: () => <span data-testid="icon-activity">A</span>,
  Clock: () => <span data-testid="icon-clock">C</span>,
  Sun: () => <span data-testid="icon-sun">Sun</span>,
  Moon: () => <span data-testid="icon-moon">Moon</span>,
}));

// Mock api to avoid network calls in tests
jest.mock("../lib/api", () => ({
  api: { get: () => Promise.resolve({ data: {} }) },
}));

describe("TopBar theme selection", () => {
  const renderWithTheme = (ui, theme = "dark") => {
    return render(
      <ThemeProvider defaultTheme={theme} attribute="data-theme" storageKey="theme">
        {ui}
      </ThemeProvider>,
    );
  };

  beforeEach(() => {
    // Clear saved theme before each test
    try { window.localStorage.removeItem("theme"); } catch {}
  });

  it("shows sun button and selects light mode", () => {
    renderWithTheme(<TopBar title="Test" />);
    const sunBtn = screen.getByTestId("theme-light");
    expect(sunBtn).toBeInTheDocument();
    fireEvent.click(sunBtn);
    expect(sunBtn).toHaveAttribute("aria-pressed", "true");
  });

  it("shows moon button and selects dark mode", () => {
    renderWithTheme(<TopBar title="Test" />, "light");
    const moonBtn = screen.getByTestId("theme-dark");
    expect(moonBtn).toBeInTheDocument();
    fireEvent.click(moonBtn);
    expect(moonBtn).toHaveAttribute("aria-pressed", "true");
  });

  it("persists theme preference across renders", () => {
    try { window.localStorage.setItem("theme", "light"); } catch {}
    renderWithTheme(<TopBar title="Test" />);
    expect(screen.getByTestId("theme-light")).toHaveAttribute("aria-pressed", "true");
  });

  it("recovers from unavailable localStorage without breaking app", () => {
    // Simulate unavailable storage by overriding setItem/getItem
    const original = window.localStorage;
    Object.defineProperty(window, "localStorage", {
      value: {
        getItem: () => { throw new Error("Storage disabled"); },
        setItem: () => { throw new Error("Storage disabled"); },
        removeItem: () => {},
      },
      writable: true,
      configurable: true,
    });
    expect(() => renderWithTheme(<TopBar title="Test" />)).not.toThrow();
    expect(screen.getByTestId("topbar")).toBeInTheDocument();
    Object.defineProperty(window, "localStorage", {
      value: original,
      writable: true,
      configurable: true,
    });
  });
});
