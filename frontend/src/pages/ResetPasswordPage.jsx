import React, { useState, useMemo } from "react";
import { Link, useSearchParams, useNavigate } from "react-router-dom";
import { toast } from "sonner";
import { api, formatApiError } from "../lib/api";
import { ArrowRight, Loader2, CheckCircle2, AlertCircle } from "lucide-react";

export default function ResetPasswordPage() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const token = params.get("token") || "";
  const [form, setForm] = useState({ password: "", confirm: "" });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [done, setDone] = useState(false);

  const tokenMissing = !token;

  const mismatch = useMemo(
    () => form.password && form.confirm && form.password !== form.confirm,
    [form.password, form.confirm]
  );

  const submit = async (e) => {
    e.preventDefault();
    setError("");
    if (form.password.length < 6) {
      setError("Password must be at least 6 characters.");
      return;
    }
    if (form.password !== form.confirm) {
      setError("Passwords do not match.");
      return;
    }
    setLoading(true);
    try {
      await api.post("/auth/reset-password", { token, new_password: form.password });
      setDone(true);
      toast.success("Password updated");
      setTimeout(() => navigate("/login"), 1600);
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
          <h1 className="text-3xl md:text-4xl font-bold tracking-tight mb-3">Choose a new password</h1>
          <p className="text-sm text-[var(--ff-text-muted)]">This link can only be used once.</p>
        </div>

        {tokenMissing ? (
          <div data-testid="reset-no-token" className="border border-[#FF3366]/30 bg-[#FF3366]/5 p-6 rounded-sm flex items-start gap-3">
            <AlertCircle size={20} strokeWidth={1.5} color="#FF3366" className="mt-0.5" />
            <div>
              <div className="font-semibold mb-1">Missing reset token</div>
              <div className="text-sm text-[var(--ff-text)]">
                This page requires a valid token. Open the reset link from your email.
              </div>
              <Link to="/forgot-password" className="inline-block mt-3 text-sm text-[#00E5FF] hover:text-[#33EFFF]">
                Request a new link →
              </Link>
            </div>
          </div>
        ) : done ? (
          <div data-testid="reset-success" className="border border-[var(--ff-border)] bg-[var(--ff-surface)] p-6 rounded-sm flex items-start gap-3">
            <CheckCircle2 size={20} strokeWidth={1.5} color="#00FF66" className="mt-0.5" />
            <div>
              <div className="font-semibold mb-1">Password updated</div>
              <div className="text-sm text-[var(--ff-text-muted)]">Redirecting to sign in…</div>
            </div>
          </div>
        ) : (
          <form
            onSubmit={submit}
            data-testid="reset-form"
            className={`border border-[var(--ff-border)] bg-[var(--ff-surface)] p-7 rounded-sm space-y-5 ${error ? "ff-shake" : ""}`}
          >
            <div className="space-y-2">
              <label className="font-mono text-[10px] uppercase tracking-widest text-[var(--ff-text-muted)]">
                New password · min 6 chars
              </label>
              <input
                data-testid="reset-password"
                type="password"
                minLength={6}
                required
                value={form.password}
                onChange={(e) => setForm({ ...form, password: e.target.value })}
                className="w-full bg-[var(--ff-input-bg)] border border-[var(--ff-border)] px-3 py-2.5 text-sm rounded-sm focus:border-[#00E5FF]"
                autoComplete="new-password"
              />
            </div>
            <div className="space-y-2">
              <label className="font-mono text-[10px] uppercase tracking-widest text-[var(--ff-text-muted)]">
                Confirm password
              </label>
              <input
                data-testid="reset-confirm"
                type="password"
                minLength={6}
                required
                value={form.confirm}
                onChange={(e) => setForm({ ...form, confirm: e.target.value })}
                className={`w-full bg-[var(--ff-input-bg)] border px-3 py-2.5 text-sm rounded-sm focus:border-[#00E5FF] ${
                  mismatch ? "border-[#FF3366]/50" : "border-[var(--ff-border)]"
                }`}
                autoComplete="new-password"
              />
              {mismatch && (
                <div className="text-[#FF3366] text-xs font-mono">Passwords do not match</div>
              )}
            </div>

            {error && (
              <div data-testid="reset-error" className="text-[#FF3366] text-sm font-mono">
                {error}
              </div>
            )}

            <button
              data-testid="reset-submit"
              type="submit"
              disabled={loading || mismatch}
              className="w-full bg-[#00E5FF] text-black font-semibold text-sm py-2.5 rounded-sm hover:bg-[#33EFFF] disabled:opacity-60 flex items-center justify-center gap-2 transition-colors"
            >
              {loading ? <Loader2 size={14} className="animate-spin" /> : <ArrowRight size={14} strokeWidth={2} />}
              {loading ? "Updating" : "Update password"}
            </button>

            <div className="pt-2 border-t border-[var(--ff-border)] text-center">
              <Link to="/login" className="text-xs text-[var(--ff-text-muted)] hover:text-[#00E5FF]" data-testid="reset-goto-login">
                Back to sign in
              </Link>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}
