/**
 * Parity guard: every `docs/*.md` (except README) must appear in the
 * hand-curated SIDEBAR, and the SIDEBAR must not point at slugs that
 * don't exist on disk. The renderer auto-loads markdown via
 * `import.meta.glob`, so a missing sidebar entry leaves the page
 * unreachable from the dashboard nav even though `/docs/<slug>` works.
 *
 * We discover the on-disk set with the same `import.meta.glob` primitive
 * `DocsPage.tsx` uses, so the test sees exactly what the build sees —
 * and stays fully browser-shaped (no node:fs, no `@types/node`).
 */
import { describe, expect, it } from "vitest";

import { SIDEBAR } from "./DocsPage";

const DOC_MODULES = import.meta.glob("../../../../docs/*.md", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

describe("DocsPage SIDEBAR", () => {
  it("matches the set of docs/*.md files (excluding README)", () => {
    const filesystemSlugs = Object.keys(DOC_MODULES)
      .map((p) => p.split("/").pop()!.replace(/\.md$/, "").toLowerCase())
      .filter((slug) => slug !== "readme")
      .sort();
    const sidebarSlugs = SIDEBAR.map((e) => e.slug).sort();
    expect(sidebarSlugs).toEqual(filesystemSlugs);
  });
});
