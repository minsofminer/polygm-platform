import { describe, expect, it } from "vitest";
import { flattenMarkdownLinks, safeSourceUrl, sanitiseUpstreamText } from "./upstream";

/** The seed's own text, verbatim: markup, a script tag, an entity, a markdown link and an event handler. It
 *  is the same string the API test asserts survives the API untouched, so the pair of tests covers the whole
 *  path from the database to the DOM. */
const HOSTILE =
  "Resolves YES if the 23:59 UTC close on <b>Coinbase</b> &amp; the Binance 1m candle both print above " +
  "$150,000. <script>steal()</script> See [the source](https://example.org/btc) for the candle used. " +
  "<img src=x onerror=alert(1)>";

describe("sanitiseUpstreamText", () => {
  it("keeps the sentence and drops every tag", () => {
    const clean = sanitiseUpstreamText(HOSTILE);
    expect(clean).toContain("Resolves YES if the 23:59 UTC close on Coinbase");
    expect(clean).toContain("Binance 1m candle");
    expect(clean).not.toContain("<");
    expect(clean).not.toContain(">");
  });

  it("drops a script BODY rather than just its tags", () => {
    const clean = sanitiseUpstreamText(HOSTILE);
    expect(clean).not.toContain("steal()");
    expect(sanitiseUpstreamText("<script>alert(1)</script>after").trim()).toBe("after");
  });

  it("decodes entities and strips again, so the result is a fixpoint with no markup in it", () => {
    const clean = sanitiseUpstreamText("A &amp; B &lt;script&gt;alert(1)&lt;/script&gt;");
    expect(clean).toBe("A & B");
    // The first version decoded entities LAST, so `&lt;script&gt;` came out as the literal text `<script>` —
    // inert in a text node, but markup in a string that a future caller could hand to something that renders
    // it. Idempotence is the property that makes that impossible to reintroduce quietly.
    expect(sanitiseUpstreamText(clean)).toBe(clean);
  });

  it("turns line and block boundaries into a space but keeps inline emphasis invisible", () => {
    expect(sanitiseUpstreamText("a<br/>b<b>c</b>")).toBe("a bc");
    expect(sanitiseUpstreamText("<p>one</p><p>two</p>")).toBe("one two");
    expect(sanitiseUpstreamText("  spaced   out  ")).toBe("spaced out");
  });

  it("leaves ordinary prose exactly as written", () => {
    const plain = "Resolves YES if the motion passes on the recorded vote; abstentions do not count.";
    expect(sanitiseUpstreamText(plain)).toBe(plain);
  });
});

describe("safeSourceUrl", () => {
  it("accepts http and https", () => {
    expect(safeSourceUrl("https://example.org/btc")).toBe("https://example.org/btc");
    expect(safeSourceUrl("http://example.org")).toBe("http://example.org/");
  });

  it("refuses everything else, including the schemes that look like links", () => {
    for (const hostile of ["javascript:alert(1)", "data:text/html,<script>alert(1)</script>",
                           "vbscript:msgbox(1)", "file:///etc/passwd", "https://localhost",
                           "not a url", "//example.org/path"]) {
      expect(safeSourceUrl(hostile)).toBeNull();
    }
  });
});

describe("flattenMarkdownLinks", () => {
  it("shows the text and the destination instead of rendering a link the creator chose", () => {
    expect(flattenMarkdownLinks("See [the source](https://example.org/btc) for the candle."))
      .toBe("See the source (https://example.org/btc) for the candle.");
  });

  it("keeps only the text when the destination is unsafe, even when it contains parentheses", () => {
    expect(flattenMarkdownLinks("See [click](javascript:alert(1)) here.")).toBe("See click here.");
    expect(flattenMarkdownLinks("[wiki](https://example.org/Definition_(mathematics))"))
      .toBe("wiki (https://example.org/Definition_(mathematics))");
  });
});
