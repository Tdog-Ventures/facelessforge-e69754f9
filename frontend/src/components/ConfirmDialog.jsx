import React, { createContext, useContext, useState, useCallback } from "react";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "./ui/alert-dialog";

/**
 * Dark, app-themed replacement for window.confirm.
 *
 * Usage:
 *   const confirm = useConfirm();
 *   const ok = await confirm({
 *     title: "Regenerate share link?",
 *     description: "The old URL will stop working immediately.",
 *     confirmLabel: "Regenerate",
 *     tone: "warning",
 *   });
 *   if (!ok) return;
 */

const ConfirmContext = createContext(null);

export function ConfirmProvider({ children }) {
  const [state, setState] = useState({ open: false });

  const confirm = useCallback((opts = {}) => {
    return new Promise((resolve) => {
      setState({
        open: true,
        title: opts.title || "Are you sure?",
        description: opts.description || "",
        confirmLabel: opts.confirmLabel || "Confirm",
        cancelLabel: opts.cancelLabel || "Cancel",
        tone: opts.tone || "primary", // primary | warning | destructive
        resolve,
      });
    });
  }, []);

  const close = (result) => {
    state.resolve?.(result);
    setState({ open: false });
  };

  const toneClasses = {
    primary: "bg-[#00E5FF] text-black hover:bg-[#33EFFF] border-[#00E5FF]",
    warning: "bg-[#FFB020] text-black hover:bg-[#FFC247] border-[#FFB020]",
    destructive: "bg-[#FF3366] text-white hover:bg-[#FF547E] border-[#FF3366]",
  }[state.tone] || "bg-[#00E5FF] text-black hover:bg-[#33EFFF] border-[#00E5FF]";

  return (
    <ConfirmContext.Provider value={confirm}>
      {children}
      <AlertDialog
        open={state.open}
        onOpenChange={(o) => { if (!o) close(false); }}
      >
        <AlertDialogContent
          data-testid="confirm-dialog"
          className="bg-[#121212] border border-zinc-800 rounded-sm max-w-md"
        >
          <AlertDialogHeader>
            <AlertDialogTitle className="text-white text-base font-semibold tracking-tight">
              {state.title}
            </AlertDialogTitle>
            {state.description && (
              <AlertDialogDescription className="text-zinc-400 text-sm leading-relaxed">
                {state.description}
              </AlertDialogDescription>
            )}
          </AlertDialogHeader>
          <AlertDialogFooter className="pt-2 border-t border-zinc-800">
            <AlertDialogCancel
              data-testid="confirm-dialog-cancel"
              onClick={() => close(false)}
              className="bg-transparent border border-zinc-800 text-zinc-300 hover:bg-[#1A1A1A] hover:text-white rounded-sm text-sm font-medium"
            >
              {state.cancelLabel}
            </AlertDialogCancel>
            <AlertDialogAction
              data-testid="confirm-dialog-confirm"
              onClick={() => close(true)}
              className={`${toneClasses} border font-semibold text-sm rounded-sm transition-colors`}
            >
              {state.confirmLabel}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </ConfirmContext.Provider>
  );
}

export function useConfirm() {
  const ctx = useContext(ConfirmContext);
  if (!ctx) {
    // Fallback to window.confirm if provider is missing, so components stay safe.
    // eslint-disable-next-line no-alert
    return async (opts = {}) => window.confirm(opts.description || opts.title || "Are you sure?");
  }
  return ctx;
}
