/**
 * Upstream text, treated as hostile.
 *
 * `resolutionCriteria` is written by whoever created the market. It is the one string on the page an outsider
 * controls, and it arrives from the API VERBATIM on purpose (the API's own test asserts the `<script>` tag is
 * still in the payload) so that the escaping is proved here rather than assumed to have happened in Python.
 *
 * Two functions, both pure, both testable:
 *
 *   - `sanitiseUpstreamText` STRIPS markup instead of escaping it. Escaping would be safer but reads badly:
 *     the creator's sentence comes through as `&lt;b&gt;Coinbase&lt;/b&gt;`, and a reader who cannot see the
 *     intent stops trusting the field. Stripping keeps the words, drops the tags, and drops the contents of
 *     `script`/`style` entirely (a stripped tag can still leave its payload behind as visible text).
 *   - `safeSourceUrl` allows only `http`/`https` with a real host, so `javascript:`, `data:` and
 *     protocol-relative URLs cannot be dressed up as the resolution source.
 *
 * Neither function is a replacement for React's own escaping: the result is rendered as a text node, so a
 * `<` that survives stripping is still inert. This is defence in depth, and the test asserts the depth (the
 * fixture includes a script tag, an event handler, an entity and a markdown link).
 */

const COMMENT = /<!--[\s\S]*?-->/g;
const SCRIPT_BODY = /<(script|style|iframe|object|embed|svg|math)[\s\S]*?<\/\1>/gi;
const VOID_OR_BLOCK = /<(br|hr|\/?p|\/?div|\/?li|\/?ul|\/?ol|\/?h[1-6]|\/?table|\/?tr|\/?td)[^>]*>/gi;
const STILL_A_TAG = /<[^>]*>/g;

function decodeEntities(text: string): string {
  return text
    .replace(/&nbsp;/gi, " ")
    .replace(/&amp;/gi, "&")
    .replace(/&lt;/gi, "<")
    .replace(/&gt;/gi, ">")
    .replace(/&quot;/gi, '"')
    .replace(/&#39;|&apos;/gi, "'");
}

function stripMarkup(text: string): string {
  let out = text;
  let previous = "";
  // Loop to a fixpoint: `<<script>script>` style nesting defeats a single pass, and the loop is bounded by the
  // string shrinking each round.
  while (previous !== out) {
    previous = out;
    out = out.replace(SCRIPT_BODY, " ");
  }
  // Inline emphasis tags disappear (`<b>Coinbase</b>` is the word), while line breaks and block boundaries
  // become a space, because joining two paragraphs would silently change what the criteria say.
  return out.replace(VOID_OR_BLOCK, " ").replace(STILL_A_TAG, "");
}

export function sanitiseUpstreamText(input: string): string {
  // Order matters, and it was wrong in the first version: decoding entities LAST left `&lt;script&gt;` as the
  // literal text `<script>` in the output, which is inert in a text node but is markup sitting in a string that
  // a future caller could hand to something that renders it. So the pipeline is strip -> decode -> strip AGAIN,
  // and the second strip is what makes this function a fixpoint: sanitising an already-sanitised string returns
  // it unchanged, which the test asserts, because "safe once" is not a property you can reason about later.
  // Three stages, and the MIDDLE one is the correction: decode entities BETWEEN two stripping passes.
  // Decoding last (the first version) emitted the literal text `<script>`; decoding once and stripping once
  // left a script's body on screen as prose. Stripping on both sides of the decode means anything that BECOMES
  // markup is treated as markup, and the function is a fixpoint, which the test asserts.
  const stripped = stripMarkup(input.replace(COMMENT, " "));
  const decoded = decodeEntities(stripped);
  return stripMarkup(decoded).replace(/\s+/g, " ").trim();
}

export function safeSourceUrl(input: string): string | null {
  let url: URL;
  try {
    url = new URL(input);
  } catch {
    return null;
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") return null;
  if (!url.hostname.includes(".")) return null;
  return url.toString();
}

/** Markdown links in the criteria are shown as their text plus the destination, rather than rendered as
 *  links: a resolution source must be ONE link the rail controls, not a set of links the creator chose. */
export function flattenMarkdownLinks(input: string): string {
  // The href group allows ONE level of balanced parentheses, because a URL may contain them
  // (`.../Definition_(mathematics)`) and — more to the point — an unsafe scheme like `javascript:alert(1)`
  // does. The first version stopped at the first `)`, so it read the destination as `javascript:alert(1` and
  // left a stray `)` in the visible sentence: a sanitiser that mangles its input is one nobody keeps using.
  return input.replace(/\[([^\]]+)\]\(((?:[^()\s]|\([^)]*\))+)\)/g, (_, text: string, href: string) => {
    const safe = safeSourceUrl(href);
    return safe ? `${text} (${safe})` : text;
  });
}
