import "@testing-library/jest-dom";
import fs from "fs";
import path from "path";
import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { ThemeProvider } from "next-themes";
import CreateProjectPage from "./CreateProjectPage";
import { api } from "../lib/api";
import { lengthStops } from "../lib/length";

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

const mockNavigate = jest.fn();
jest.mock(
  "react-router-dom",
  () => ({
    useNavigate: () => mockNavigate,
    NavLink: ({ children, to, className, onClick, "data-testid": testId }) => {
      const active = false;
      const cls = typeof className === "function" ? className({ isActive: active }) : className;
      return (
        <a href={to} className={cls} onClick={onClick} data-testid={testId}>
          {typeof children === "function" ? children({ isActive: active }) : children}
        </a>
      );
    },
  }),
  { virtual: true },
);

jest.mock("../lib/auth", () => ({
  useAuth: () => ({
    user: { id: "u1", name: "Test User", role: "user" },
    logout: jest.fn(),
  }),
}));

jest.mock("../lib/api", () => ({
  api: {
    get: jest.fn(() => Promise.resolve({ data: { status: "ok" } })),
    post: jest.fn(() => Promise.resolve({ data: { id: "proj-123", name: "Test Project" } })),
  },
  formatApiError: (d) => (typeof d === "string" ? d : "Error"),
}));

jest.mock("sonner", () => ({
  toast: {
    success: jest.fn(),
    error: jest.fn(),
  },
}));

const renderPage = (theme = "light") =>
  render(
    <ThemeProvider defaultTheme={theme} attribute="data-theme" storageKey="theme">
      <CreateProjectPage />
    </ThemeProvider>,
  );

describe("CreateProjectPage theme, accessibility, and semantic token guardrails", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    api.post.mockImplementation(() => Promise.resolve({ data: { id: "proj-123", name: "Test Project" } }));
    api.get.mockImplementation(() => Promise.resolve({ data: { status: "ok" } }));
  });

  it("forbids hard-coded dark card/input/text classes in CreateProjectPage.jsx and uses shared semantic tokens", () => {
    const source = fs.readFileSync(path.join(__dirname, "CreateProjectPage.jsx"), "utf8");

    // Must not reintroduce hard-coded dark backgrounds, zinc borders/text, or raw hex colors
    expect(source).not.toMatch(/bg-\[#121212\]/i);
    expect(source).not.toMatch(/bg-\[#0A0A0A\]/i);
    expect(source).not.toMatch(/border-zinc-[0-9]+/);
    expect(source).not.toMatch(/text-zinc-[0-9]+/);
    expect(source).not.toMatch(/hover:text-white/);
    expect(source).not.toMatch(/#[0-9a-fA-F]{3,6}/);

    // Must use shared semantic form classes / tokens
    expect(source).toMatch(/ff-form-card/);
    expect(source).toMatch(/ff-input/);
    expect(source).toMatch(/ff-select/);
    expect(source).toMatch(/ff-checkbox/);
  });

  it("defines shared theme form tokens, color-scheme, and autofill protection in index.css", () => {
    const css = fs.readFileSync(path.join(__dirname, "..", "index.css"), "utf8");

    expect(css).toMatch(/:root\s*\{[\s\S]*?color-scheme:\s*dark/);
    expect(css).toMatch(/\[data-theme="light"\]\s*\{[\s\S]*?color-scheme:\s*light/);
    expect(css).toMatch(/--ff-input-bg:/);
    expect(css).toMatch(/--ff-input-text:/);
    expect(css).toMatch(/--ff-input-border:/);
    expect(css).toMatch(/--ff-input-placeholder:/);
    expect(css).toMatch(/\.ff-form-card\b/);
    expect(css).toMatch(/\.ff-input\b/);
    expect(css).toMatch(/\.ff-select\b/);
    expect(css).toMatch(/\.ff-checkbox\b/);
    expect(css).toMatch(/:-webkit-autofill/);
  });

  it("associates every form control with an accessible label in editorial and commercial modes", () => {
    renderPage("light");

    // Core editorial controls
    expect(screen.getByLabelText(/Project name/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/YouTube niche/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/Video topic/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/Who's watching/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/Tone/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/Voice style/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/Visual style/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/^Length/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/What's the goal/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/What should they do/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/This is a paid client ad \(commercial\)/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/Auto-post to social when the video is ready/i)).toBeInTheDocument();

    // Expand commercial mode and auto-post
    fireEvent.click(screen.getByTestId("field-commercial"));
    fireEvent.click(screen.getByLabelText(/Auto-post to social when the video is ready/i));

    expect(screen.getByLabelText(/Client business name/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/Advertised service/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/^Offer$/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/Client audience/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/^Location$/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/Verified CTA text/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/Verified CTA destination/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/I verified this CTA with the client/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/Auto-post to Instagram when ready/i)).toBeInTheDocument();
  });

  it("marks invalid fields with aria-invalid and links error messages via aria-describedby", () => {
    renderPage("light");

    fireEvent.click(screen.getByTestId("field-commercial"));
    fireEvent.click(screen.getByTestId("create-project-submit"));

    const nameInput = screen.getByTestId("field-name");
    const nameError = screen.getByTestId("error-name");
    expect(nameInput).toHaveAttribute("aria-invalid", "true");
    expect(nameError).toHaveAttribute("id", "error-name");
    expect(nameInput.getAttribute("aria-describedby")).toContain("error-name");

    const clientInput = screen.getByTestId("field-client-name");
    const clientError = screen.getByTestId("error-client_name");
    expect(clientInput).toHaveAttribute("aria-invalid", "true");
    expect(clientError).toHaveAttribute("id", "error-client_name");
    expect(clientInput.getAttribute("aria-describedby")).toContain("error-client_name");
  });

  it("preserves editorial and commercial payload submission and navigation behaviour", async () => {
    renderPage("dark");

    fireEvent.change(screen.getByTestId("field-name"), { target: { value: "Dark Mode Study" } });
    fireEvent.change(screen.getByTestId("field-niche"), { target: { value: "design" } });
    fireEvent.change(screen.getByTestId("field-topic"), { target: { value: "How contrast tokens prevent invisible text regressions" } });
    fireEvent.change(screen.getByTestId("field-audience"), { target: { value: "Frontend engineers" } });
    expect(screen.getByTestId("length-readout")).toHaveTextContent("5 minutes");
    fireEvent.change(screen.getByTestId("field-duration"), {
      target: { value: String(lengthStops().indexOf(60)) },
    });
    expect(screen.getByTestId("length-readout")).toHaveTextContent("1 minute");

    fireEvent.click(screen.getByTestId("create-project-submit"));

    await waitFor(() => {
      expect(api.post).toHaveBeenCalledWith(
        "/projects",
        expect.objectContaining({
          name: "Dark Mode Study",
          niche: "design",
          topic: "How contrast tokens prevent invisible text regressions",
          audience: "Frontend engineers",
          target_duration: 60,
          production_mode: "editorial",
          client_name: "",
          structured_brief: null,
          verified_cta: null,
          brand_assets_supplied: false,
        }),
      );
    });
    expect(mockNavigate).toHaveBeenCalledWith("/app/projects/proj-123");
  });

  it("validates and submits commercial projects, and supports Cancel navigation", async () => {
    renderPage("light");

    fireEvent.click(screen.getByTestId("field-commercial"));
    fireEvent.click(screen.getByTestId("create-project-submit"));

    expect(screen.getByTestId("error-client_name")).toHaveTextContent("Client name is required");
    expect(screen.getByTestId("error-brief_service")).toHaveTextContent("Required for a commercial brief");
    expect(screen.getByTestId("error-verified_cta")).toHaveTextContent("Enter and verify the CTA destination");
    expect(api.post).not.toHaveBeenCalled();

    fireEvent.change(screen.getByTestId("field-name"), { target: { value: "Effective Wash Promo" } });
    fireEvent.change(screen.getByTestId("field-niche"), { target: { value: "home services" } });
    fireEvent.change(screen.getByTestId("field-topic"), { target: { value: "Why driveway pressure washing boosts curb appeal" } });
    fireEvent.change(screen.getByTestId("field-audience"), { target: { value: "Adelaide homeowners" } });
    fireEvent.change(screen.getByTestId("field-client-name"), { target: { value: "Effective Wash" } });
    fireEvent.change(screen.getByTestId("field-brief-service"), { target: { value: "Pressure washing" } });
    fireEvent.change(screen.getByTestId("field-brief-offer"), { target: { value: "Free driveway quote" } });
    fireEvent.change(screen.getByTestId("field-brief-audience"), { target: { value: "Adelaide homeowners" } });
    fireEvent.change(screen.getByTestId("field-brief-location"), { target: { value: "Adelaide, SA" } });
    fireEvent.change(screen.getByTestId("field-verified-cta-text"), { target: { value: "Call for a free quote" } });
    fireEvent.change(screen.getByTestId("field-verified-cta-destination"), { target: { value: "https://effectivewash.example" } });
    fireEvent.click(screen.getByTestId("field-verified-cta-checkbox"));

    fireEvent.click(screen.getByTestId("create-project-submit"));

    await waitFor(() => {
      expect(api.post).toHaveBeenCalledWith(
        "/projects",
        expect.objectContaining({
          name: "Effective Wash Promo",
          production_mode: "commercial_ad",
          client_name: "Effective Wash",
          structured_brief: expect.objectContaining({
            service: "Pressure washing",
            offer: "Free driveway quote",
            audience: "Adelaide homeowners",
            location: "Adelaide, SA",
          }),
          verified_cta: {
            text: "Call for a free quote",
            destination: "https://effectivewash.example",
            verified: true,
          },
        }),
      );
    });

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(mockNavigate).toHaveBeenCalledWith("/app/projects");
  });

  it("shows seconds under a minute and minutes from one minute up, with no second chips", () => {
    renderPage("light");
    expect(screen.queryByRole("group", { name: "Target duration presets" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "300s" })).not.toBeInTheDocument();
    fireEvent.change(screen.getByTestId("field-duration"), {
      target: { value: String(lengthStops().indexOf(45)) },
    });
    expect(screen.getByTestId("length-readout")).toHaveTextContent("45 seconds");
    fireEvent.change(screen.getByTestId("field-duration"), {
      target: { value: String(lengthStops().indexOf(600)) },
    });
    expect(screen.getByTestId("length-readout")).toHaveTextContent("10 minutes");
    expect(screen.queryByText("600s")).not.toBeInTheDocument();
  });
});
