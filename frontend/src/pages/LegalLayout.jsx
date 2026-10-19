import React from "react";
import { Link } from "react-router-dom";

export default function LegalLayout({ title, updated, sections }) {
  const serif = { fontFamily: "Georgia, 'Iowan Old Style', 'Times New Roman', serif" };
  return (
    <div style={{ background: "#EDEFF2", color: "#12161C", minHeight: "100vh", fontFamily: "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif" }}>
      <header className="max-w-3xl mx-auto px-6 py-5 flex items-center justify-between">
        <Link to="/" className="font-semibold text-lg" style={serif}>FacelessForge</Link>
        <Link to="/login" className="text-sm underline" style={{ color: "#4A5361" }}>Log in</Link>
      </header>
      <main className="max-w-3xl mx-auto px-6 py-10">
        <h1 className="text-4xl font-semibold tracking-tight" style={serif}>{title}</h1>
        <p className="mt-2 text-sm" style={{ color: "#4A5361" }}>Last updated {updated}</p>
        {sections.map(([h, body]) => (
          <section key={h} className="mt-8">
            <h2 className="text-xl font-semibold" style={serif}>{h}</h2>
            <p className="mt-2 leading-relaxed" style={{ color: "#2B323C" }}>{body}</p>
          </section>
        ))}
      </main>
      <footer className="max-w-3xl mx-auto px-6 py-10 text-sm flex gap-6" style={{ color: "#4A5361" }}>
        <Link to="/privacy" className="underline">Privacy policy</Link>
        <Link to="/terms" className="underline">Terms of service</Link>
        <Link to="/" className="underline">Home</Link>
      </footer>
    </div>
  );
}
