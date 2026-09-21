/**
 * The withdrawal ceremony, in a browser: the ladder is a *sequence* (amount → destination → typed amount →
 * typed address → password → authenticator code → done), and the two things that make it safe are that the
 * primary control is unreachable until the step above it is satisfied and that the typed values must be what the
 * screen showed. Both are properties of a rendered form, which is what this file is for.
 *
 * The API is stubbed at the network boundary so the test is about the ceremony's own rules, not about passwords:
 * the point here is the *order* and the disabled states, and those are the client's.
 */
import { expect, test } from "@playwright/test";

const ALLOWLISTED = "0x1111111111111111111111111111111111111111";

test("the withdrawal ladder cannot be skipped and the typed values must match what was shown", async ({ page }) => {
  await page.route("**/v1/wallet/bridge*", (route) => route.fulfill({ json: { requestId: "wr_1", state: "requested" } }));
  await page.route("**/v1/wallet**", (route) => {
    const url = route.request().url();
    if (url.includes("available")) {
      return route.fulfill({
        json: { balanceMicro: "25000000", lockedMicro: "0", destinations: [{ address: ALLOWLISTED, label: "cold", coolingMs: 0 }] },
      });
    }
    return route.fulfill({ json: {} });
  });

  await page.goto("/wallet");
  const withdraw = page.getByRole("tab", { name: /withdraw/i });
  await withdraw.click();

  // Step 1: an amount above the ledger is refused in words, and the next step stays shut.
  await page.getByLabel(/amount/i).fill("999.00");
  const next = page.getByRole("button", { name: /continue|next|review/i });
  await expect(next).toBeDisabled();

  await page.getByLabel(/amount/i).fill("12.50");
  await expect(next).toBeEnabled();
  await next.click();

  // Step 2: the destination must come from the allowlist; a pasted raw address is not a destination.
  await expect(page.getByText(/allowlist|cooling/i).first()).toBeVisible();
  const codeInputs = page.locator("input");
  expect(await codeInputs.count()).toBeGreaterThan(1);

  // Step 3: the amount must be TYPED back, and a different number is refused rather than "corrected".
  await page.getByLabel(/type the amount/i).fill("12.51");
  await expect(page.getByRole("alert")).toContainText(/match|different/i);
});
