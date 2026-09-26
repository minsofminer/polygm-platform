/**
 * The disclosure is copy, so most of what can go wrong with it is textual — and the one thing that must not happen
 * is that a future edit softens it. These tests read the rules from `config/gtm.json` (the same file the P16 gate
 * reads) rather than restating them, so "the required phrases are present" means the same thing in both places.
 *
 * The last two tests are source-level on purpose: they assert that the three surfaces actually render the
 * component. A disclaimer component that nothing imports is a disclaimer nobody sees, and that failure is
 * invisible to every other kind of test.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { DISCLAIMER_LINES, DisclaimerFooter } from "./disclaimer";

const ROOT = resolve(__dirname, "../../..");
const gtm = JSON.parse(readFileSync(resolve(ROOT, "config/gtm.json"), "utf8"));
const required: Record<string, string> = gtm.copy_rules.required_phrases;
const banned: string[] = gtm.copy_rules.banned;

const joined = DISCLAIMER_LINES.join(" ").toLowerCase();

describe("the words that have to be there", () => {
  it("carries every required phrase verbatim", () => {
    for (const [rule, phrase] of Object.entries(required)) {
      expect(joined, `required phrase for ${rule} is missing from the disclaimer`).toContain(phrase.toLowerCase());
    }
  });

  it("says the three things in the order that makes the argument", () => {
    expect(DISCLAIMER_LINES.length).toBe(3);
    expect(DISCLAIMER_LINES[0]!.toLowerCase()).toContain("not affiliated");
    expect(DISCLAIMER_LINES[1]!.toLowerCase()).toContain("market, not a forecast");
    expect(DISCLAIMER_LINES[2]!.toLowerCase()).toContain("lose everything you deposit");
  });

  it("contains none of the banned words", () => {
    for (const word of banned) {
      expect(joined, `the banned phrase "${word}" reached the disclaimer`).not.toContain(word.toLowerCase());
    }
  });

  it("states the loss plainly rather than hedging it", () => {
    // The dishonest version of this sentence is "markets can be volatile" — true, useless, and exactly what the
    // phase's constraint about implied profits is written against.
    expect(joined).toContain("resolve to zero");
    expect(joined).not.toContain("potential");
  });
});

describe("the component that renders it", () => {
  it("renders one paragraph per line", () => {
    const { container } = render(<DisclaimerFooter />);
    const lines = container.querySelectorAll("p.pgm-disclaimer__line");
    expect(lines.length).toBe(DISCLAIMER_LINES.length);
    lines.forEach((p, i) => expect(p.textContent).toBe(DISCLAIMER_LINES[i]));
  });

  it("is real markup with a role, not a visual-only block", () => {
    const { container } = render(<DisclaimerFooter />);
    const footer = container.querySelector("footer.pgm-disclaimer");
    expect(footer).not.toBeNull();
    expect(footer!.getAttribute("role")).toBe("contentinfo");
    expect(footer!.getAttribute("aria-label")).toBeTruthy();
  });

  it("is server-safe: no hooks, no client directive", () => {
    const src = readFileSync(resolve(__dirname, "disclaimer.tsx"), "utf8");
    expect(src).not.toContain('"use client"');
    expect(src).not.toMatch(/\buse(State|Effect|Memo|Callback)\b/);
  });
});

describe("the surfaces that have to show it", () => {
  const surfaces: Array<[string, string]> = [
    ["the landing page", "app/page.tsx"],
    ["the Mini App", "src/tma/TmaScreen.tsx"],
    ["the public page frame", "src/public/Chrome.tsx"],
  ];

  for (const [name, rel] of surfaces) {
    it(`${name} renders it`, () => {
      const src = readFileSync(resolve(ROOT, "web", rel), "utf8");
      expect(src, `${rel} must import the shared disclaimer`).toContain("@/legal/disclaimer");
      expect(src, `${rel} must render it, not just import it`).toMatch(/<DisclaimerFooter(\s|\/|>)/);
    });
  }

  it("keeps the required-phrase list in config as the only source", () => {
    // If the phrases are restated here as well as in the config, the two will drift and this file will keep
    // passing while the gate fails — which is the worst of both.
    const self = readFileSync(__filename, "utf8");
    for (const phrase of Object.values(required)) {
      expect(self, "the phrases must be read from config, not retyped here").not.toContain(`"${phrase}"`);
    }
  });
});
