// P08 tailwind config. It has one job: consume the generated preset and declare what the app's
// components look like to Tailwind. Any colour, size, duration or z-index written here is a bug —
// `tools/p03-gate-check.py` G6.8 scans this file for colour literals and `tools/p08-gate-check.py` c6
// refuses the build if a theme value appears here instead of in brand/tokens.json.
const preset = require("./tailwind.preset.cjs");

/** @type {import('tailwindcss').Config} */
module.exports = {
  presets: [preset],
  content: ["./app/**/*.{ts,tsx}", "./src/**/*.{ts,tsx}"],
  darkMode: ["selector", '[data-theme="dark"]'],
  theme: {
    extend: {
      // Nothing new. `extend` here would be a second copy of the design system, which is the failure mode
      // P03's gate calls "redefined, not consumed" (web/DESIGN.md §1).
    },
  },
  plugins: [],
};
