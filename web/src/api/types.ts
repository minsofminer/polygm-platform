/**
 * The API's request/response shapes, taken from `src/api/schema.gen.ts`, which `npm run gen:api` produces
 * from `contracts/openapi.yaml` and `npm run check:api` refuses to let drift. This module is the only place a
 * caller can spell a route's body type, and it contains no hand-written field names: the prompt's "no
 * hand-written API types" is a claim about where types come from, and a grep answers it.
 */
import type { components, paths } from "./schema.gen";

export type Schemas = components["schemas"];
export type Market = Schemas["Market"];
export type Price = Schemas["Price"];
export type Fill = Schemas["Fill"];
export type DurableFill = Schemas["DurableFill"];
export type Levels = Schemas["Levels"];
export type Stamped = Schemas["Stamped"];
export type Candle = Schemas["Candle"];
export type EventSummary = Schemas["EventSummary"];
export type Level = Schemas["Levels"][number];

type Method = "get" | "post" | "put" | "patch" | "delete";

/** The body of one documented response, or `Record<string, unknown>` when it documents none. */
type RespBody<R> = R extends { content?: { "application/json"?: infer Body } }
  ? Body extends undefined | never
    ? Record<string, unknown>
    : Body
  : Record<string, unknown>;

/**
 * The documented 2xx response keys of an operation. Both spellings are listed because openapi-typescript emits
 * the YAML's quoted `"200"` as the NUMERIC key `200`, and a `Extract<keyof R, \`2${string}\`>` matches neither
 * — it silently yields `never`, which is how a body type becomes `unknown` while every check still passes.
 */
type OkKeys<R> = Extract<
  keyof R,
  200 | 201 | 202 | 203 | 204 | 205 | 206 | 207 | 208 | 226 | "200" | "201" | "202" | "203" | "204" | "205" | "206" | "207" | "208" | "226"
>;

/**
 * The 2xx response schema for a path, or `Record<string, unknown>` when the contract documents no body for it —
 * the auth routes are description-only in the contract today, and `Record<string, unknown>` says that out loud
 * instead of pretending.
 *
 * 2xx only, deliberately: an earlier version unioned every documented response, so the body type of a GET was
 * `{bids, asks, ...} | {error: {...}}` and every field access on a successful body was a type error. The union
 * was "correct" (those are all the things the route can return) and useless (a caller that has a 200 does not
 * have an error envelope).
 */
export type SuccessBody<Path extends keyof paths, M extends Method> =
  paths[Path] extends Record<M, infer Op>
    ? Op extends { responses: infer R }
      ? R extends Record<string, unknown>
        ? OkKeys<R> extends never
          ? Record<string, unknown>
          : RespBody<R[OkKeys<R>]>
        : Record<string, unknown>
      : Record<string, unknown>
    : Record<string, unknown>;
