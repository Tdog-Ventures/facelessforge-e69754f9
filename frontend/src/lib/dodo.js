import { api } from "./api";

// Dodo product IDs - set via REACT_APP_DODO_PRODUCT_* or fallback to billing_dodo.py defaults
// Configure these once Dodo products are created in dashboard. Fallbacks are pdt_* placeholders.
export const DODO_PRODUCT_IDS = {
  basic: process.env.REACT_APP_DODO_PRODUCT_STARTER || process.env.REACT_APP_DODO_PRODUCT_BASIC || "pdt_starter",
  pro: process.env.REACT_APP_DODO_PRODUCT_CREATOR || process.env.REACT_APP_DODO_PRODUCT_PRO || "pdt_creator",
  advanced: process.env.REACT_APP_DODO_PRODUCT_PRO || process.env.REACT_APP_DODO_PRODUCT_ADVANCED || "pdt_pro",
  topup_100: process.env.REACT_APP_DODO_PRODUCT_TOPUP_100 || "pdt_topup_100",
  topup_300: process.env.REACT_APP_DODO_PRODUCT_TOPUP_300 || "pdt_topup_300",
};

export async function createDodoCheckout(productId, quantity = 1) {
  const { data } = await api.post("/billing/dodo/checkout", {
    product_id: productId,
    quantity,
  });
  if (!data?.checkout_url) throw new Error("No checkout_url returned");
  return data;
}
