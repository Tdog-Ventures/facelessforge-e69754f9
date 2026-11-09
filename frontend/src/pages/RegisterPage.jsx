import React, { useState } from "react";
import { useNavigate, Link } from "react-router-dom";
import { useAuth } from "../lib/auth";
import { Loader2, ArrowRight } from "lucide-react";

export default function RegisterPage() {
  const { register } = useAuth();
  const navigate = useNavigate();
  const [form, setForm] = useState({ name: "", email: "", password: "", role: "creator" });
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const update = (k) => (e) => setForm({ ...form, [k]: e.target.value });

  const submit = async (e) => {
    e.preventDefault();
    setError("");
    setLoading(true);
    const res = await register(form);
    setLoading(false);
    if (res.ok) navigate("/app");
    else setError(res.error);
  };

  return (
    <div className="min-h-screen ff-grid bg-[var(--ff-input-bg)] text-[var(--ff-text)] flex items-center justify-center px-6">
      <div className="w-full max-w-md">
        <div className="mb-8 text-center">
          <h1 className="text-3xl md:text-4xl font-bold tracking-tight mb-3">
            Create your studio
          </h1>
          <p className="text-sm text-[var(--ff-text-muted)]">
            You'll be set up as a Creator. Admins can promote you later.
          </p>
        </div>

        <form
          onSubmit={submit}
          data-testid="register-form"
          className={`border border-[var(--ff-border)] bg-[var(--ff-surface)] p-7 rounded-sm space-y-5 ${error ? "ff-shake" : ""}`}
        >
          <div className="space-y-2">
            <label className="font-mono text-[10px] uppercase tracking-widest text-[var(--ff-text-muted)]">Name</label>
            <input data-testid="register-name" required value={form.name} onChange={update("name")}
              className="w-full bg-[var(--ff-input-bg)] border border-[var(--ff-border)] px-3 py-2.5 text-sm rounded-sm focus:border-[#00E5FF]" />
          </div>
          <div className="space-y-2">
            <label className="font-mono text-[10px] uppercase tracking-widest text-[var(--ff-text-muted)]">Email</label>
            <input data-testid="register-email" type="email" required value={form.email} onChange={update("email")}
              className="w-full bg-[var(--ff-input-bg)] border border-[var(--ff-border)] px-3 py-2.5 text-sm rounded-sm focus:border-[#00E5FF]" />
          </div>
          <div className="space-y-2">
            <label className="font-mono text-[10px] uppercase tracking-widest text-[var(--ff-text-muted)]">Password</label>
            <input data-testid="register-password" type="password" minLength={6} required value={form.password} onChange={update("password")}
              className="w-full bg-[var(--ff-input-bg)] border border-[var(--ff-border)] px-3 py-2.5 text-sm rounded-sm focus:border-[#00E5FF]" />
          </div>
          <div className="space-y-2">
            <label className="font-mono text-[10px] uppercase tracking-widest text-[var(--ff-text-muted)]">Role</label>
            <select data-testid="register-role" value={form.role} onChange={update("role")}
              className="w-full bg-[var(--ff-input-bg)] border border-[var(--ff-border)] px-3 py-2.5 text-sm rounded-sm focus:border-[#00E5FF]">
              <option value="creator">Creator</option>
              <option value="editor">Editor</option>
              <option value="viewer">Viewer</option>
            </select>
          </div>

          {error && <div data-testid="register-error" className="text-[#FF3366] text-sm font-mono">{error}</div>}

          <button
            data-testid="register-submit"
            type="submit"
            disabled={loading}
            className="w-full bg-[#00E5FF] text-black font-semibold text-sm py-2.5 rounded-sm hover:bg-[#33EFFF] disabled:opacity-60 flex items-center justify-center gap-2 transition-colors"
          >
            {loading ? <Loader2 size={14} className="animate-spin" /> : <ArrowRight size={14} strokeWidth={2} />}
            {loading ? "Creating" : "Create account"}
          </button>

          <div className="pt-2 border-t border-[var(--ff-border)] text-center">
            <span className="text-xs text-[var(--ff-text-muted)]">Already have one? </span>
            <Link to="/login" className="text-xs text-[#00E5FF] hover:text-[#33EFFF]" data-testid="goto-login">
              Sign in
            </Link>
          </div>
        </form>
      </div>
    </div>
  );
}
