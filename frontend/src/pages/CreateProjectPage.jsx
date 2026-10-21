import React, { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Loader2, Sparkles } from "lucide-react";
import { toast } from "sonner";
import AppShell from "../components/AppShell";
import TopBar from "../components/TopBar";
import { api, formatApiError } from "../lib/api";

const PLATFORMS = [
  { key: "instagram", label: "Instagram" },
  { key: "facebook", label: "Facebook" },
  { key: "threads", label: "Threads" },
];

const TONES = ["calm-authoritative", "curious-expert", "cinematic", "energetic", "mysterious", "warm-narrative"];
const VOICE_STYLES = ["neutral male narrator", "neutral female narrator", "deep male narrator", "warm female narrator", "young male narrator"];
const VISUAL_STYLES = ["cinematic b-roll", "moody minimal", "motion graphic heavy", "archival + painterly", "anime-inspired", "stock + text cards"];
const MONETISATION = ["Grow channel to 50k subs", "Sell my product", "Build authority", "ads", "ads + affiliate", "affiliate", "digital product", "sponsorship"];
const CTAS = ["Visit website", "Subscribe", "Buy product", "subscribe", "join newsletter", "visit website", "buy product", "book call"];

const DURATIONS = [5, 15, 30, 60, 120, 300, 600, 1800, 3600];

const inputClass = "w-full bg-[#0A0A0A] border border-zinc-800 px-3 py-2.5 text-sm rounded-sm focus:border-[#00E5FF]";
const selectClass = inputClass + " font-mono uppercase text-xs tracking-wider";

const Field = ({ label, name, children, hint, errors }) => (
  <div className="space-y-2">
    <label className="font-mono text-[10px] uppercase tracking-widest text-zinc-500 flex items-center justify-between">
      <span>{label}</span>
      {hint && <span className="text-zinc-600 normal-case tracking-normal">{hint}</span>}
    </label>
    {children}
    {errors[name] && <div className="text-[#FF3366] text-xs font-mono" data-testid={`error-${name}`}>{errors[name]}</div>}
  </div>
);

export default function CreateProjectPage() {
  const navigate = useNavigate();
  const [form, setForm] = useState({
    name: "",
    niche: "",
    topic: "",
    audience: "",
    tone: TONES[0],
    target_duration: 300,
    voice_style: VOICE_STYLES[0],
    visual_style: VISUAL_STYLES[0],
    monetisation_intent: MONETISATION[1],
    cta_goal: CTAS[0],
    auto_post: false,
    platforms: PLATFORMS.map((p) => p.key),
    production_mode: "editorial",
    client_name: "",
    structured_brief: { service: "", offer: "", audience: "", location: "", problem: "" },
    verified_cta: { text: "", destination: "", verified: false },
    brand_assets_supplied: false,
    aspect_ratio: "9:16",
    resolution: "1080x1920",
  });
  const [errors, setErrors] = useState({});
  const [loading, setLoading] = useState(false);

  const update = (k, v) => setForm({ ...form, [k]: v });
  const isCommercial = form.production_mode === "commercial_ad";

  const validate = () => {
    const e = {};
    if (!form.name.trim()) e.name = "Name is required";
    if (form.niche.trim().length < 3) e.niche = "At least 3 characters";
    if (form.topic.trim().length < 10) e.topic = "At least 10 characters";
    if (!form.audience.trim()) e.audience = "Audience is required";
    if (!form.tone) e.tone = "Pick a tone";
    const d = Number(form.target_duration);
    if (!(d >= 5 && d <= 3600)) e.target_duration = "5–3600 seconds";
    if (isCommercial) {
      if (!form.client_name.trim()) e.client_name = "Client name is required";
      for (const key of ["service", "offer", "audience", "location"]) {
        if (!form.structured_brief[key].trim()) e[`brief_${key}`] = "Required for a commercial brief";
      }
      if (!form.verified_cta.text.trim() || !form.verified_cta.destination.trim() || !form.verified_cta.verified) e.verified_cta = "Enter and verify the CTA destination";
    }
    setErrors(e);
    return Object.keys(e).length === 0;
  };

  const submit = async (e) => {
    e.preventDefault();
    if (!validate()) return;
    setLoading(true);
    try {
      const payload = { ...form, target_duration: Number(form.target_duration) };
      if (!isCommercial) {
        payload.client_name = "";
        payload.structured_brief = null;
        payload.verified_cta = null;
        payload.brand_assets_supplied = false;
      }
      const { data } = await api.post("/projects", payload);
      toast.success("Project created", { description: data.name });
      navigate(`/app/projects/${data.id}`);
    } catch (err) {
      toast.error("Could not create project", { description: formatApiError(err.response?.data?.detail) || err.message });
    } finally {
      setLoading(false);
    }
  };

  return (
    <AppShell>
      <TopBar title="Create Project" subtitle="New video package" />

      <div className="p-8">
        <form onSubmit={submit} data-testid="create-project-form" className="max-w-4xl space-y-8">
          <div className="border border-zinc-800 bg-[#121212] p-6 rounded-sm space-y-5">
            <div className="flex items-center gap-2 text-[#00E5FF] mb-2">
              <Sparkles size={14} strokeWidth={1.5} />
              <span className="font-mono text-[10px] uppercase tracking-widest">Step 1 · Core idea</span>
            </div>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
              <Field label="Project name" name="name" errors={errors}>
                <input data-testid="field-name" value={form.name} onChange={(e) => update("name", e.target.value)}
                  className={inputClass} placeholder="e.g. The Hidden Psychology of Dark Mode" />
              </Field>
              <Field label="YouTube niche" name="niche" hint="≥ 3 chars" errors={errors}>
                <input data-testid="field-niche" value={form.niche} onChange={(e) => update("niche", e.target.value)}
                  className={inputClass} placeholder="finance, design, history…" />
              </Field>
            </div>
            <Field label="Video topic" name="topic" hint="≥ 10 chars" errors={errors}>
              <textarea data-testid="field-topic" value={form.topic} onChange={(e) => update("topic", e.target.value)} rows={3}
                className={inputClass + " resize-none"} placeholder="What exactly will this video explain?" />
            </Field>
            <Field label="Who's watching? ⓘ" name="audience" hint="Who will watch this? This shapes voice + examples" errors={errors}>
              <input data-testid="field-audience" value={form.audience} onChange={(e) => update("audience", e.target.value)}
                className={inputClass} placeholder="e.g. Gen Z tech hobbyists, busy pros" />
            </Field>
          </div>

          <div className="border border-zinc-800 bg-[#121212] p-6 rounded-sm space-y-4">
            <div className="flex items-start gap-3">
              <input
                id="commercial-mode"
                data-testid="field-commercial"
                type="checkbox"
                checked={isCommercial}
                onChange={(e) => update("production_mode", e.target.checked ? "commercial_ad" : "editorial")}
                className="mt-0.5 h-4 w-4 accent-[#00E5FF] bg-[#0A0A0A] border-zinc-700 rounded"
              />
              <div>
                <label htmlFor="commercial-mode" className="text-sm text-zinc-200">This is a paid client ad (commercial)</label>
                <p className="text-xs text-zinc-500 mt-1">Leave unchecked for an editorial YouTube video. Commercial ads require client ground truth and verified CTA details.</p>
              </div>
            </div>
          </div>

          {isCommercial && (
          <div className="border border-[#00E5FF]/30 bg-[#121212] p-6 rounded-sm space-y-5">
            <div className="flex items-center gap-2 text-[#00E5FF] mb-2">
              <Sparkles size={14} strokeWidth={1.5} />
              <span className="font-mono text-[10px] uppercase tracking-widest">Commercial ground truth · required</span>
            </div>
            <Field label="Client business name" name="client_name" errors={errors}>
              <input data-testid="field-client-name" value={form.client_name} onChange={(e) => update("client_name", e.target.value)} className={inputClass} placeholder="e.g. Effective Wash" />
            </Field>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
              {[["service", "Advertised service", "Pressure washing"], ["offer", "Offer", "Free driveway quote"], ["audience", "Client audience", "Adelaide homeowners"], ["location", "Location", "Adelaide, SA"]].map(([key, label, placeholder]) => (
                <Field key={key} label={label} name={`brief_${key}`} errors={errors}>
                  <input value={form.structured_brief[key]} onChange={(e) => update("structured_brief", { ...form.structured_brief, [key]: e.target.value })} className={inputClass} placeholder={placeholder} />
                </Field>
              ))}
            </div>
            <Field label="Verified CTA" name="verified_cta" errors={errors}>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                <input value={form.verified_cta.text} onChange={(e) => update("verified_cta", { ...form.verified_cta, text: e.target.value })} className={inputClass} placeholder="Call for a free quote" />
                <input value={form.verified_cta.destination} onChange={(e) => update("verified_cta", { ...form.verified_cta, destination: e.target.value })} className={inputClass} placeholder="https://… or tel:+61…" />
              </div>
              <label className="flex items-center gap-2 text-sm text-zinc-300 mt-3"><input type="checkbox" checked={form.verified_cta.verified} onChange={(e) => update("verified_cta", { ...form.verified_cta, verified: e.target.checked })} /> I verified this CTA with the client.</label>
            </Field>
            <Field label="Brand assets supplied" name="brand_assets_supplied" hint="Feeds the release grounding gate" errors={errors}>
              <label className="flex items-center gap-2 text-sm text-zinc-300">
                <input
                  type="checkbox"
                  data-testid="field-brand-assets"
                  checked={form.brand_assets_supplied}
                  onChange={(e) => update("brand_assets_supplied", e.target.checked)}
                  className="h-4 w-4 accent-[#00E5FF] bg-[#0A0A0A] border-zinc-700 rounded"
                />
                The client supplied their own brand assets (logo, footage, product shots).
              </label>
            </Field>
          </div>
          )}

          <div className="border border-zinc-800 bg-[#121212] p-6 rounded-sm space-y-5">
            <div className="flex items-center gap-2 text-[#7B61FF] mb-2">
              <Sparkles size={14} strokeWidth={1.5} />
              <span className="font-mono text-[10px] uppercase tracking-widest">Step 2 · Production style</span>
            </div>
            <div className="grid grid-cols-1 md:grid-cols-3 gap-5">
              <Field label="Tone" name="tone" errors={errors}>
                <select data-testid="field-tone" value={form.tone} onChange={(e) => update("tone", e.target.value)} className={selectClass}>
                  {TONES.map((t) => <option key={t} value={t}>{t}</option>)}
                </select>
              </Field>
              <Field label="Voice style" name="voice_style" errors={errors}>
                <select data-testid="field-voice" value={form.voice_style} onChange={(e) => update("voice_style", e.target.value)} className={selectClass}>
                  {VOICE_STYLES.map((t) => <option key={t} value={t}>{t}</option>)}
                </select>
              </Field>
              <Field label="Visual style" name="visual_style" errors={errors}>
                <select data-testid="field-visual" value={form.visual_style} onChange={(e) => update("visual_style", e.target.value)} className={selectClass}>
                  {VISUAL_STYLES.map((t) => <option key={t} value={t}>{t}</option>)}
                </select>
              </Field>
            </div>
            <Field label="Target duration (seconds)" name="target_duration" hint="5 – 3600" errors={errors}>
              <div className="flex items-center gap-3">
                <input data-testid="field-duration" type="number" min={5} max={3600} value={form.target_duration}
                  onChange={(e) => update("target_duration", e.target.value)}
                  className={inputClass + " w-32 font-mono"} />
                <div className="flex gap-2">
                  {DURATIONS.map((d) => (
                    <button
                      key={d}
                      type="button"
                      onClick={() => update("target_duration", d)}
                      className={`font-mono text-[10px] px-2 py-1 border rounded-sm uppercase tracking-widest transition-colors ${
                        Number(form.target_duration) === d
                          ? "border-[#00E5FF] text-[#00E5FF]"
                          : "border-zinc-800 text-zinc-500 hover:text-white"
                      }`}
                    >
                      {d}s
                    </button>
                  ))}
                </div>
              </div>
            </Field>
          </div>

          <div className="border border-zinc-800 bg-[#121212] p-6 rounded-sm space-y-5">
            <div className="flex items-center gap-2 text-[#FFB020] mb-2">
              <Sparkles size={14} strokeWidth={1.5} />
              <span className="font-mono text-[10px] uppercase tracking-widest">Step 3 · Goals</span>
            </div>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
              <Field label="What's the goal? ⓘ" name="monetisation_intent" hint="What do you want this video to do?" errors={errors}>
                <select data-testid="field-mon" value={form.monetisation_intent} onChange={(e) => update("monetisation_intent", e.target.value)} className={selectClass}>
                  {MONETISATION.map((t) => <option key={t} value={t}>{t}</option>)}
                </select>
              </Field>
              <Field label="What should they do? ⓘ" name="cta_goal" hint="Main action you want viewer to take" errors={errors}>
                <select data-testid="field-cta" value={form.cta_goal} onChange={(e) => update("cta_goal", e.target.value)} className={selectClass}>
                  {CTAS.map((t) => <option key={t} value={t}>{t}</option>)}
                </select>
              </Field>
            </div>
          </div>

          <div className="border border-zinc-800 bg-[#121212] p-6 rounded-sm space-y-4">
            <div className="flex items-center gap-2 text-[#00FF66] mb-2">
              <Sparkles size={14} strokeWidth={1.5} />
              <span className="font-mono text-[10px] uppercase tracking-widest">Step 4 · Distribution</span>
            </div>
            <div className="flex items-center gap-3">
              <input
                id="auto-post"
                type="checkbox"
                checked={form.auto_post}
                onChange={(e) => setForm({ ...form, auto_post: e.target.checked })}
                className="h-4 w-4 accent-[#00E5FF] bg-[#0A0A0A] border-zinc-700 rounded"
              />
              <label htmlFor="auto-post" className="text-sm text-zinc-200">
                Auto-post to social when the video is ready
              </label>
            </div>
            {form.auto_post && (
              <div className="pl-7 space-y-2">
                {PLATFORMS.map((p) => (
                  <label key={p.key} className="flex items-center gap-2 text-sm text-zinc-300">
                    <input
                      type="checkbox"
                      checked={form.platforms.includes(p.key)}
                      onChange={(e) => {
                        const next = e.target.checked
                          ? [...form.platforms, p.key]
                          : form.platforms.filter((x) => x !== p.key);
                        setForm({ ...form, platforms: next });
                      }}
                      className="h-4 w-4 accent-[#00E5FF] bg-[#0A0A0A] border-zinc-700 rounded"
                    />
                    Auto-post to {p.label} when ready
                  </label>
                ))}
              </div>
            )}
          </div>

          <div className="flex items-center gap-3">
            <button
              data-testid="create-project-submit"
              type="submit"
              disabled={loading}
              className="flex items-center gap-2 bg-[#00E5FF] text-black font-semibold text-sm px-5 py-2.5 rounded-sm hover:bg-[#33EFFF] disabled:opacity-60 transition-colors"
            >
              {loading ? <Loader2 size={14} className="animate-spin" /> : <Sparkles size={14} strokeWidth={2} />}
              {loading ? "Creating" : "Create Project"}
            </button>
            <button
              type="button"
              onClick={() => navigate("/app/projects")}
              className="text-sm text-zinc-400 hover:text-white px-4 py-2"
            >
              Cancel
            </button>
          </div>
        </form>
      </div>
    </AppShell>
  );
}
