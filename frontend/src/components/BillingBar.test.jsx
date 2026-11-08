jest.mock("react-router-dom", () => ({ Link: () => null }), { virtual: true });
import { authoritativeBilling } from "./BillingBar";

test("a Dodo credit hash is not treated as the account balance", () => {
  expect(authoritativeBilling({ credits: 0, status: "UNKNOWN", tenant_id: "u1" })).toBeNull();
});

test("an operator balance stays authoritative", () => {
  const balance = authoritativeBilling({
    operator_unmetered: true, remaining: 0, monthly: 150, plan: "free",
  });
  expect(balance.operator_unmetered).toBe(true);
  expect(balance.remaining).toBe(0);
});
