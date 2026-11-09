import React, { useEffect, useRef } from "react";
import { Link } from "react-router-dom";

const INK = "#12161C";
const PAPER = "#EDEFF2";
const SLATE = "#4A5361";
const RULE = "#C9CFD8";
const REC = "#D6361F";

const serif = { fontFamily: "Georgia, 'Iowan Old Style', 'Times New Roman', serif" };
const sans = { fontFamily: "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif" };

const stages = [
  ["Script", "Writes the hook, the script and the scene list from your topic."],
  ["Voice", "Records the narration in a natural voice."],
  ["Footage", "Finds licensed stock clips that match each scene."],
  ["Captions", "Burns in subtitles and checks the sound levels."],
  ["Upload", "Sends the finished video to your YouTube channel."],
];

export default function LandingPage() {
  const videoRef = useRef(null);

  useEffect(() => {
    try { localStorage.setItem("forge_invite", "FORGE"); } catch (e) {}
    const reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduce && videoRef.current) videoRef.current.pause();
  }, []);

  const focus = "focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2";

  return (
    <div style={{ background: PAPER, color: INK, ...sans, minHeight: "100vh" }}>
      <header className="max-w-6xl mx-auto px-6 py-5 flex items-center justify-between">
        <Link to="/" className={"font-semibold text-lg " + focus} style={serif}>FacelessForge</Link>
        <nav className="flex items-center gap-6 text-sm" style={{ color: SLATE }}>
          <Link to="/login" className={"hover:underline " + focus}>Log in</Link>
          <Link to="/register" className={"px-4 py-2 rounded-md text-[var(--ff-text)] " + focus} style={{ background: INK }}>Create an account</Link>
        </nav>
      </header>

      <main>
        <section className="max-w-6xl mx-auto px-6 pt-10 pb-20 grid md:grid-cols-[1.25fr_1fr] gap-12 items-center">
          <div>
            <h1 className="text-4xl md:text-6xl leading-[1.05] font-semibold tracking-tight" style={serif}>
              A finished YouTube video, every day, without filming anything.
            </h1>
            <p className="mt-6 text-lg leading-relaxed max-w-xl" style={{ color: SLATE }}>
              Give FacelessForge a topic. It writes the script, records the voiceover, adds footage and captions, and uploads the video to your channel on the schedule you set.
            </p>
            <div className="mt-8 flex flex-wrap items-center gap-4">
              <Link to="/register" className={"px-6 py-3 rounded-md text-[var(--ff-text)] font-medium " + focus} style={{ background: REC }}>Create an account</Link>
              <Link to="/login" className={"px-6 py-3 rounded-md font-medium border " + focus} style={{ borderColor: INK }}>Log in</Link>
            </div>
          </div>

          <div className="mx-auto w-full max-w-[300px]">
            <div className="rounded-[28px] p-2" style={{ background: INK }}>
              <video
                ref={videoRef}
                autoPlay muted loop playsInline
                src="/hero-render.mp4"
                aria-label="A video made by FacelessForge"
                className="w-full rounded-[22px] block"
                style={{ aspectRatio: "9 / 16", objectFit: "cover", background: "#2A303A" }}
              />
            </div>
            <p className="mt-3 text-xs text-center" style={{ color: SLATE }}>A video made by FacelessForge, unedited.</p>
          </div>
        </section>

        <section className="border-t" style={{ borderColor: RULE }}>
          <div className="max-w-6xl mx-auto px-6 py-16">
            <h2 className="text-2xl md:text-3xl font-semibold" style={serif}>What happens between your topic and your channel</h2>
            <div className="mt-10 grid grid-cols-1 md:grid-cols-5 gap-x-6 gap-y-8">
              {stages.map(([name, body]) => (
                <div key={name}>
                  <div className="h-2 rounded-full mb-4" style={{ background: name === "Upload" ? REC : INK }} />
                  <h3 className="font-semibold">{name}</h3>
                  <p className="mt-2 text-sm leading-relaxed" style={{ color: SLATE }}>{body}</p>
                </div>
              ))}
            </div>
          </div>
        </section>

        <section id="youtube" className="border-t" style={{ borderColor: RULE }}>
          <div className="max-w-3xl mx-auto px-6 py-16">
            <h2 className="text-2xl md:text-3xl font-semibold" style={serif}>How FacelessForge uses your YouTube account</h2>
            <p className="mt-5 leading-relaxed" style={{ color: SLATE }}>
              When you connect YouTube, FacelessForge asks for one permission: to upload videos to your channel. It uses that permission only to upload videos you generated, either when you press upload or on a schedule you turn on.
            </p>
            <p className="mt-4 leading-relaxed" style={{ color: SLATE }}>
              It does not read your watch history, your subscriptions or any other part of your Google account. You can disconnect inside the app at any time, and you can also remove access at myaccount.google.com/permissions.
            </p>
            <p className="mt-4 leading-relaxed" style={{ color: SLATE }}>
              FacelessForge use and transfer of information received from Google APIs adheres to the{" "}
              <a className={"underline " + focus} href="https://developers.google.com/terms/api-services-user-data-policy">Google API Services User Data Policy</a>, including the Limited Use requirements.
            </p>
          </div>
        </section>
      </main>

      <footer className="border-t" style={{ borderColor: RULE }}>
        <div className="max-w-6xl mx-auto px-6 py-8 flex flex-wrap items-center justify-between gap-4 text-sm" style={{ color: SLATE }}>
          <span>FacelessForge by ETHINX Systems, Adelaide, South Australia</span>
          <span className="flex gap-6">
            <Link to="/privacy" className={"underline " + focus}>Privacy policy</Link>
            <Link to="/terms" className={"underline " + focus}>Terms of service</Link>
            <Link to="/license" className={"underline " + focus}>License</Link>
          </span>
        </div>
      </footer>
    </div>
  );
}
