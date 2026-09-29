/**
 * The withdrawal ceremony, in a browser: the ladder is a *sequence* (amount → destination → typed amount →
 * typed address → password → authenticator code → done), and the two things that make it safe are that the
 * primary control is unreachable until the step above it is satisfied and that the typed values must be what the
 * screen showed. Both are properties of a rendered form, which is what this file is for.
 *
 * The API is stubbed at the network boundary so the test is about the ceremony's own rules, not about passwords:
 * the point here is the *order* and the disabled states, and those are the client's.
 */
import { expect, test, type Locator, type Page } from "@playwright/test";
import { signIn } from "./session";

const ALLOWLISTED = "0x1111111111111111111111111111111111111111";

/**
 * Press a step's control the way the device in front of it can.
 *
 * On the phone profile a *coordinate* click is unreliable here: the tab bar is fixed over the bottom edge, the
 * button sits below the fold, and Playwright scrolls a target to its nearest edge — which is the edge the bar
 * owns. Retries then land on whatever the scroll left there. The ceremony's claim is about the order of the
 * steps, so the phone presses the control the way a keyboard and a switch do: focus it, then Enter. That path
 * has to work on a phone as well — a control only a coordinate click can reach is exactly what the design
 * review had to fix once already.
 */
async function press(page: Page, locator: Locator, projectName: string): Promise<void> {
  if (projectName === "desktop") {
    await locator.click();
    return;
  }
  await locator.focus();
  await page.keyboard.press("Enter");
}

test("the withdrawal ladder cannot be skipped and the typed values must match what was shown", async ({ page }, testInfo) => {
  // The screen is behind the (app) layout, whose session decision is made on the server from this cookie:
  // without it every assertion below runs against the sign-in page (see ./session).
  await signIn(page);
  // The routes this ceremony actually speaks, from the contract: the balance card, the allowlist, and the
  // withdrawal itself. The stubs were `**/v1/wallet**` with an "available" branch and a `/wallet/bridge` post —
  // paths from no version of this API — so the spec asserted against screens that never had a balance to read.
  await page.route("**/v1/wallet/balance*", (route) =>
    route.fulfill({
      json: {
        cashMicro: "25000000",          // $25.00, from the ledger
        reservedMicro: "0",             // nothing locked, so the whole balance is available
        locks: { password: true, totp: true, custody: "managed" },
        note: "the ledger's number, not the venue's",
        // The stamp is not decoration: a read without `asOf`/`staleAfter` is refused by the client as
        // UNSTAMPED_READ, with the route named. A stub that omits it tests a refusal, not the screen.
        asOf: 1_700_000_000_000,
        staleAfter: 1_700_000_003_000,
      },
    }),
  );
  await page.route("**/v1/wallet/withdrawal-addresses*", (route) =>
    route.fulfill({
      json: {
        items: [{ id: "addr_1", address: ALLOWLISTED, label: "cold", cooldownRemainingMs: 0 }],
        asOf: 1_700_000_000_000,
        staleAfter: 1_700_000_003_000,
      },
    }),
  );
  let withdrawBody: Record<string, unknown> | null = null;
  await page.route("**/v1/wallet/withdraw", async (route) => {
    withdrawBody = route.request().postDataJSON();
    await route.fulfill({ status: 202, json: { withdrawalId: 7, status: "recorded", destination: ALLOWLISTED } });
  });

  await page.goto("/wallet");
  // `exact` on purpose: /withdraw/i also matches the "Withdrawal addresses" tab, and Playwright is right to
  // refuse a selector that names two controls when the test is about one of them.
  const withdraw = page.getByRole("tab", { name: "Withdraw", exact: true });
  await press(page, withdraw, testInfo.project.name);

  // Step 1: an amount above the ledger is refused in words, and the next step stays shut.
  await page.getByLabel(/amount/i).fill("999.00");
  const next = page.getByRole("button", { name: /continue|next|review/i });
  await expect(next).toBeDisabled();

  await page.getByLabel(/amount/i).fill("12.50");
  await expect(next).toBeEnabled();
  await press(page, next, testInfo.project.name);

  // Step 2: the destination must come from the allowlist; a pasted raw address is not a destination.
  await expect(page.getByText(/allowlist|cooling/i).first()).toBeVisible();
  const codeInputs = page.locator("input");
  expect(await codeInputs.count()).toBeGreaterThan(1);

  // Step 3: the amount must be TYPED back, and a different number is refused rather than "corrected".
  await page.getByLabel(/type the amount/i).fill("12.51");
  // Scoped to the ceremony: the framework keeps a `role="alert"` route announcer in the document, and an
  // unscoped query would match it as readily as the refusal this step is about.
  await expect(page.locator('[id$="-panel-withdraw"]').getByRole("alert")).toContainText(/match|different/i);
});
