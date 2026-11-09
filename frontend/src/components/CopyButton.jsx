import React, { useState } from "react";
import { toast } from "sonner";
import { Copy, Check } from "lucide-react";

export default function CopyButton({ text, label = "Copy", testId }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text || "");
      setCopied(true);
      toast.success("Copied to clipboard");
      setTimeout(() => setCopied(false), 1500);
    } catch {
      toast.error("Copy failed");
    }
  };
  return (
    <button
      type="button"
      onClick={copy}
      data-testid={testId || "copy-btn"}
      className="inline-flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-widest text-[var(--ff-text-muted)] hover:text-[#00E5FF] border border-[var(--ff-border)] hover:border-[#00E5FF] px-2 py-1 rounded-sm transition-colors"
    >
      {copied ? <Check size={12} strokeWidth={1.8} /> : <Copy size={12} strokeWidth={1.5} />}
      {copied ? "Copied" : label}
    </button>
  );
}
