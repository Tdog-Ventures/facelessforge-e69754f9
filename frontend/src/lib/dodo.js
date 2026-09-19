// Dodo Payments - checkout via backend /api/billing/dodo/checkout
// Replaces Paddle client-side SDK. Flow: POST to backend with JWT, redirect to checkout_url
import { api } from "./api";

export const creditsMap = { basic: 150, pro: 600, advanced: 2000 };

// Dodo product IDs - set via REACT_APP_DODO_PRODUCT_* or fallback to billing_dodo.py defaults
// Configure these once Dodo products are created in dashboard. Fallbacks are pdt_* placeholders.
export const DODO_PRODUCT_IDS = {
  basic: process.env.REACT_APP_DODO_PRODUCT_STARTER || process.env.REACT_APP_DODO_PRODUCT_BASIC || "pdt_starter",
  pro: process.env.REACT_APP_DODO_PRODUCT_CREATOR || process.env.REACT_APP_DODO_PRODUCT_PRO || "pdt_creator",
  advanced: process.env.REACT_APP_DODO_PRODUCT_PRO || process.env.REACT_APP_DODO_PRODUCT_ADVANCED || "pdt_pro",
  topup_100: process.env.REACT_APP_DODO_PRODUCT_TOPUP_100 || "pdt_topup_100",
  topup_300: process.env.REACT_APP_DODO_PRODUCT_TOPUP_300 || "pdt_topup_300",
};

// Backward compat: keep PRI_IDS alias so old imports don't break during transition (empty, deprecated)
export const PRI_IDS = {};
export const PROD_IDS = {};

export async function createDodoCheckout(productId, quantity = 1) {
  const { data } = await api.post("/billing/dodo/checkout", {
    product_id: productId,
    quantity,
  });
  if (!data?.checkout_url) throw new Error("No checkout_url returned");
  return data;
}

export async function redirectToDodoCheckout(productId, quantity = 1) {
  const { checkout_url } = await createDodoCheckout(productId, quantity);
  window.location.href = checkout_url;
  return checkout_url;
}

// Keep getPaddle shim for any lingering imports - throws helpful error
export const getPaddle = async () => {
  throw new Error("Paddle removed - use Dodo Payments via createDodoCheckout()");
};
