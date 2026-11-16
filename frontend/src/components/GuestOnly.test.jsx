import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import GuestOnly from "./GuestOnly";


jest.mock("../lib/auth", () => ({
  useAuth: () => ({
    user: { id: "other-user", email: "other@example.com", role: "creator" },
  }),
}));

jest.mock("react-router-dom", () => ({
  Navigate: ({ to }) => <div data-testid="redirect">{to}</div>,
}), { virtual: true });


test("an authenticated browser keeps a password-reset token link", () => {
  window.history.pushState({}, "", "/reset-password?token=secret-token");
  const html = renderToStaticMarkup(
    <GuestOnly>
      <main data-testid="reset-page">Reset password</main>
    </GuestOnly>
  );

  expect(html).toContain('data-testid="reset-page"');
  expect(html).not.toContain("/app");
});
