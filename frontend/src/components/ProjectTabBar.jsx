import React, { useEffect, useId, useRef, useState } from "react";
import { ChevronDown } from "lucide-react";

export const CORE_TABS = [
  { id: "overview", label: "Overview" },
  { id: "script", label: "Script" },
  { id: "scenes", label: "Scenes" },
  { id: "metadata", label: "Metadata" },
  { id: "thumbnails", label: "Thumbnails" },
  { id: "voiceover", label: "Voiceover" },
  { id: "render", label: "Render & Export" },
];

export const RECORD_TABS = [
  { id: "intake", label: "Intake" },
  { id: "claims", label: "Claims" },
  { id: "rights", label: "Rights" },
  { id: "compliance", label: "Checklist" },
  { id: "approval", label: "Approval" },
  { id: "receipt", label: "Receipt" },
];

function tabClass(active) {
  return `px-4 py-3 text-sm font-medium border-b-2 transition-colors whitespace-nowrap ${
    active ? "border-[#00E5FF] text-[var(--ff-text)]" : "border-transparent text-[var(--ff-text-muted)] hover:text-[var(--ff-text)]"
  }`;
}

export default function ProjectTabBar({ tab, onChange }) {
  const [open, setOpen] = useState(false);
  const menuId = useId();
  const rootRef = useRef(null);
  const triggerRef = useRef(null);
  const itemRefs = useRef([]);
  const activeRecord = RECORD_TABS.find((item) => item.id === tab);

  useEffect(() => {
    if (!open) return undefined;
    const closeOnOutside = (event) => {
      if (!rootRef.current?.contains(event.target)) setOpen(false);
    };
    document.addEventListener("pointerdown", closeOnOutside);
    return () => document.removeEventListener("pointerdown", closeOnOutside);
  }, [open]);

  const focusItem = (index) => {
    const next = (index + RECORD_TABS.length) % RECORD_TABS.length;
    itemRefs.current[next]?.focus();
  };

  const onTriggerKeyDown = (event) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      setOpen(true);
      const index = event.key === "ArrowUp" ? RECORD_TABS.length - 1 : 0;
      setTimeout(() => focusItem(index), 0);
    }
  };

  const onItemKeyDown = (event, index) => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      focusItem(index + 1);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      focusItem(index - 1);
    } else if (event.key === "Escape") {
      event.preventDefault();
      setOpen(false);
      triggerRef.current?.focus();
    } else if (event.key === "Home") {
      event.preventDefault();
      focusItem(0);
    } else if (event.key === "End") {
      event.preventDefault();
      focusItem(RECORD_TABS.length - 1);
    }
  };

  return (
    <div ref={rootRef} className="border-b border-[rgba(59,130,246,0.15)] flex items-stretch min-w-0 max-w-full">
      <div className="relative min-w-0 flex-1">
        <div data-testid="project-tab-row" className="ff-tab-scroll flex items-center gap-1 overflow-x-auto">
          {CORE_TABS.map((item) => (
            <button
              key={item.id}
              type="button"
              data-testid={`tab-${item.id}`}
              onClick={() => onChange(item.id)}
              className={tabClass(tab === item.id)}
            >
              {item.label}
            </button>
          ))}
        </div>
        <div
          aria-hidden="true"
          data-testid="project-tab-fade"
          className="pointer-events-none absolute right-0 top-0 bottom-0 w-8 bg-gradient-to-l from-[var(--ff-bg)] to-transparent"
        />
      </div>
      <div className="relative shrink-0">
        <button
          ref={triggerRef}
          type="button"
          data-testid="tab-records"
          className={`${tabClass(Boolean(activeRecord))} inline-flex items-center gap-1`}
          aria-haspopup="menu"
          aria-expanded={open}
          aria-controls={menuId}
          aria-label={activeRecord ? `Records, ${activeRecord.label} open` : "Records"}
          onClick={() => setOpen((value) => !value)}
          onKeyDown={onTriggerKeyDown}
        >
          {activeRecord ? `Records · ${activeRecord.label}` : "Records"}
          <ChevronDown size={14} aria-hidden="true" />
        </button>
        {open && (
          <div
            id={menuId}
            role="menu"
            aria-label="Records"
            className="absolute right-0 z-40 mt-1 min-w-[12rem] rounded-md border border-[var(--ff-border)] bg-[var(--ff-surface)] p-1 shadow-md"
          >
            {RECORD_TABS.map((item, index) => (
              <button
                key={item.id}
                ref={(node) => { itemRefs.current[index] = node; }}
                type="button"
                role="menuitem"
                data-testid={`tab-${item.id}`}
                aria-current={tab === item.id ? "page" : undefined}
                className="block w-full text-left px-3 py-2 text-sm rounded-sm text-[var(--ff-text)] hover:bg-[var(--ff-bg)] focus:bg-[var(--ff-bg)]"
                onClick={() => {
                  onChange(item.id);
                  setOpen(false);
                }}
                onKeyDown={(event) => onItemKeyDown(event, index)}
              >
                {item.label}
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
