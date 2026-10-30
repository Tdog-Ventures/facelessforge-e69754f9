import React from "react";
import { Navigate } from "react-router-dom";

import { useAuth } from "../lib/auth";


export default function GuestOnly({ children }) {
  const { user } = useAuth();
  if (user === null) {
    return (
      <div className="min-h-screen bg-[#0A0A0A] text-zinc-500 font-mono text-sm flex items-center justify-center">
        Loading…
      </div>
    );
  }

  const isPasswordResetLink = typeof window !== "undefined"
    && window.location.pathname === "/reset-password"
    && new URLSearchParams(window.location.search).has("token");

  if (user && !isPasswordResetLink) return <Navigate to="/app" replace />;
  return children;
}
