import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import path from "node:path";

// The shell's own quality gate (`npm run test`) runs here. The components that carry money or safety
// semantics are the ones under test on purpose: the marketing page is not worth a unit test, a price cell
// that can flash the wrong direction is.
export default defineConfig({
  plugins: [react()],
  resolve: { alias: { "@": path.resolve(process.cwd(), "src") } },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    // The tape harness is deliberately not a 60fps assertion: no browser exists in this environment.
    // It counts React renders per update instead, which is the number that predicts dropped frames.
    testTimeout: 20_000,
  },
});
