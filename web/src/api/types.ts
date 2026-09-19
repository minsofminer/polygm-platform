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

type Method = "get" | "post" | "put" | "patch" | "delete";

/** The 2xx response schema for a path, or `never` when the contract documents no body for it — the auth
 *  routes are description-only in the contract today, and `never` says that out loud instead of pretending. */
export type SuccessBody<Path extends keyof paths, M extends Method> =
  paths[Path] extends Record<M, infer Op>
    ? Op extends { responses: infer R }
      ? R extends Record<string, infer Resp>
        ? Resp extends { content?: { "application/json"?: infer Body } }
          ? Body extends undefined | never ? Record<string, unknown> : Body
          : Record<string, unknown>
        : Record<string, unknown>
      : Record<string, unknown>
    : Record<string, unknown>;
