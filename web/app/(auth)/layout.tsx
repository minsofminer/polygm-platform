import type { ReactNode } from "react";
export default function AuthLayout({ children }: { children: ReactNode }) {
  return (
    <main
      style={{
        padding: "var(--pgm-pad-screen-compact)",
        maxWidth: "var(--pgm-shell-max-width)",
        margin: "0 auto",
        display: "grid",
        gap: "var(--pgm-space-4)",
      }}
    >
      {children}
    </main>
  );
}
