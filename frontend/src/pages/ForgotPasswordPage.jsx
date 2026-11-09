import React, { useState } from "react";
import { Link } from "react-router-dom";
import { api, formatApiError } from "../lib/api";
import { ArrowRight, ArrowLeft, Loader2, CheckCircle2 } from "lucide-react";

export default function ForgotPasswordPage() {
  const [email, setEmail] = useState("");
  const [loading, setLoading] = useState(false);
  const [sent, setSent] = useState(null); // null | {message, dev_reset_url?, dev_expires_in_minutes?}
  const [error, setError] = useState("");

  const submit = async (e) => {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      const { data } = await api.post("/auth/forgot-password", { email });
      setSent(data);
    } catch (err) {
      setError(formatApiError(err.response?.data?.detail) || err.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen ff-grid bg-[var(--ff-input-bg)] text-[var(--ff-text)] flex items-center justify-center px-6">
      <div className="w-full max-w-md">
        <div className="mb-8 text-center">
          <h1 className="text-3xl md:text-4xl font-bold tracking-tight mb-3">Reset your password</h1>
          <p className="text-sm text-[var(--ff-text-muted)]">
            Enter your email and we'll send a reset link if an account exists.
          </p>
        </div>

        {sent ? (
          <div
            data-testid="forgot-success"
            className="border border-[var(--ff-border)] bg-[var(--ff-surface)] p-7 rounded-sm space-y-5"
          >
            <div className="flex items-start gap-3">
              <CheckCircle2 size={20} strokeWidth={1.5} color="#00FF66" className="mt-0.5" />
              <div>
                <div className="font-semibold text-[var(--ff-text)] mb-1">Check your inbox</div>
                <div className="text-sm text-[var(--ff-text-muted)] leading-relaxed">{sent.message}</div>
              </div>
            </div>

            {sent.dev_reset_url && (
              <div className="border border-dashed border-[#FFB020]/40 bg-[#FFB020]/5 p-4 rounded-sm">
                <div className="font-mono text-[10px] uppercase tracking-widest text-[#FFB020] mb-2">
                  Dev mode · email delivery simulated
                </div>
                <div className="text-xs text-[var(--ff-text)] mb-2">
                  Reset link (valid for {sent.dev_expires_in_minutes} min):
                </div>
                <Link
                  to={sent.dev_reset_url.replace(/^https?:\/\/[^/]+/, "") || "/reset-password"}
                  data-testid="forgot-dev-link"
                  className="block font-mono text-[11px] text-[#00E5FF] break-all hover:underline"
                >
                  {sent.dev_reset_url}
                </Link>
              </div>
            )}

            <Link
              to="/login"
              data-testid="forgot-back-to-login"
              className="inline-flex items-center gap-2 text-sm text-[var(--ff-text-muted)] hover:text-[#00E5FF] transition-colors"
            >
              <ArrowLeft size={14} strokeWidth={1.5} /> Back to sign in
            </Link>
          </div>
        ) : (
          <form
            onSubmit={submit}
            data-testid="forgot-form"
            className={`border border-[var(--ff-border)] bg-[var(--ff-surface)] p-7 rounded-sm space-y-5 ${error ? "ff-shake" : ""}`}
          >
            <div className="space-y-2">
              <label className="font-mono text-[10px] uppercase tracking-widest text-[var(--ff-text-muted)]">Email</label>
              <input
                data-testid="forgot-email"
                type="email"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                className="w-full bg-[var(--ff-input-bg)] border border-[var(--ff-border)] px-3 py-2.5 text-sm rounded-sm focus:border-[#00E5FF]"
                autoComplete="email"
              />
            </div>

            {error && (
              <div data-testid="forgot-error" className="text-[#FF3366] text-sm font-mono">{error}</div>
            )}

            <button
              data-testid="forgot-submit"
              type="submit"
              disabled={loading}
              className="w-full bg-[#00E5FF] text-black font-semibold text-sm py-2.5 rounded-sm hover:bg-[#33EFFF] disabled:opacity-60 flex items-center justify-center gap-2 transition-colors"
            >
              {loading ? <Loader2 size={14} className="animate-spin" /> : <ArrowRight size={14} strokeWidth={2} />}
              {loading ? "Sending" : "Send reset link"}
            </button>

            <div className="pt-2 border-t border-[var(--ff-border)] text-center">
              <Link to="/login" className="text-xs text-[var(--ff-text-muted)] hover:text-[#00E5FF]" data-testid="forgot-goto-login">
                Back to sign in
              </Link>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}
