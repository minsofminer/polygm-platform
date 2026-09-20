/**
 * GENERATED — do not edit. `node tools/build-startapp-contract.mjs` writes this file from
 * `contracts/startapp.json`, which is the single source both this app and the Python bot read.
 *
 * The contract is mirrored rather than imported because the web project cannot import above its own root. The mirror
 * is verified (`npm run gen:startapp -- --check`, run by `pretest`), so a change to the grammar that is not a change
 * to both sides fails the suite instead of shipping a link that the parser on the other side rejects.
 */

export type StartappContract = {
  readonly grammar: string;
  readonly separator: string;
  readonly tags: Readonly<Record<string, string>>;
  readonly valueCharsetLiteral: string;
  readonly valueMaxLen: number;
  readonly payloadMaxLen: number;
  readonly examples: Readonly<Record<string, string>>;
};

export const STARTAAPP_CONTRACT: StartappContract = {
  "grammar": "<tag>-<value>",
  "separator": "-",
  "tags": {
    "m": "market slug",
    "w": "trader address",
    "r": "referral code"
  },
  "valueCharsetLiteral": "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-",
  "valueMaxLen": 120,
  "payloadMaxLen": 256,
  "examples": {
    "market": "m-fed-cut-sept",
    "trader": "w-0x4bbeEB066eD09B7AEd07bF39EEe0466f9EB3",
    "referral": "r-AB12"
  }
} as const;
