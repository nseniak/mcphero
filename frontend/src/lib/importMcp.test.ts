/**
 * Coverage for the pure helpers behind the MCP import dialog: grouping
 * rows by (scope, project), default selection, live id validation, and
 * building the confirm payload from selected rows + edited ids.
 */
import { describe, expect, it } from "vitest";

import type { ImportEntry } from "../api/types";
import {
  buildConfirmEntries,
  computeIdErrors,
  defaultIds,
  defaultSelectedKeys,
  groupEntries,
  normalizePreviewResponse,
  normalizeResultResponse,
  rowKey,
} from "./importMcp";

function makeEntry(
  partial: Partial<ImportEntry> & { original_id: string },
): ImportEntry {
  return {
    scope: "project",
    project_path: "/p/web",
    group_label: "web",
    proposed_id: partial.original_id,
    display_name: partial.original_id,
    transport: "streamable_http",
    auth_mode: "service_account",
    blocked: false,
    blocked_reason: null,
    duplicate_of: null,
    ...partial,
  };
}

describe("groupEntries", () => {
  it("keeps same-basename projects in separate groups", () => {
    const entries = [
      makeEntry({ original_id: "x", project_path: "/p1/web", proposed_id: "x-web" }),
      makeEntry({ original_id: "y", project_path: "/p2/web", proposed_id: "y-web" }),
    ];
    const groups = groupEntries(entries);
    expect(groups.length).toBe(2);
    expect(groups.map((g) => g.projectPath)).toEqual(["/p1/web", "/p2/web"]);
  });

  it("groups rows that share scope + project together, in order", () => {
    const user = makeEntry({ original_id: "s", scope: "user", project_path: null, group_label: "User scope", proposed_id: "s" });
    const a = makeEntry({ original_id: "a", proposed_id: "a-web" });
    const b = makeEntry({ original_id: "b", proposed_id: "b-web" });
    const groups = groupEntries([user, a, b]);
    expect(groups.map((g) => g.label)).toEqual(["User scope", "web"]);
    expect(groups[1].entries.map((e) => e.original_id)).toEqual(["a", "b"]);
  });
});

describe("defaultSelectedKeys", () => {
  it("selects addable rows but not blocked or duplicate ones", () => {
    const a = makeEntry({ original_id: "a", proposed_id: "a" });
    const b = makeEntry({ original_id: "b", proposed_id: "b", blocked: true, blocked_reason: "no" });
    const c = makeEntry({
      original_id: "c", proposed_id: "c",
      duplicate_of: { proposed_id: "a", group_label: "web" },
    });
    const sel = defaultSelectedKeys([a, b, c]);
    expect(sel.has(rowKey(a))).toBe(true);
    expect(sel.has(rowKey(b))).toBe(false);
    expect(sel.has(rowKey(c))).toBe(false);
  });
});

describe("computeIdErrors", () => {
  it("flags empty, existing, and duplicated ids among selected rows", () => {
    const selected = new Set(["a", "b", "c", "d"]);
    const ids = new Map([
      ["a", "github"], // duplicate with d
      ["b", ""], // empty
      ["c", "mixpanel"], // collides with an existing upstream
      ["d", "github"], // duplicate with a
    ]);
    const errors = computeIdErrors(selected, ids, new Set(["mixpanel"]));
    expect(errors.get("a")).toBe("duplicate");
    expect(errors.get("b")).toBe("empty");
    expect(errors.get("c")).toBe("exists");
    expect(errors.get("d")).toBe("duplicate");
  });

  it("ignores unselected rows", () => {
    const errors = computeIdErrors(
      new Set(["a"]),
      new Map([["a", "ok"], ["b", ""]]),
      new Set(),
    );
    expect(errors.size).toBe(0);
  });
});

describe("buildConfirmEntries", () => {
  it("emits only selected rows, using the edited id as target", () => {
    const a = makeEntry({ original_id: "a", proposed_id: "a-web" });
    const b = makeEntry({ original_id: "b", proposed_id: "b-web" });
    const selected = new Set([rowKey(a)]);
    const ids = new Map([[rowKey(a), "custom"], [rowKey(b), "b-web"]]);
    expect(buildConfirmEntries([a, b], selected, ids)).toEqual([
      { scope: "project", project_path: "/p/web", original_id: "a", target_id: "custom" },
    ]);
  });
});

// Regression for MCPOLIS-FRONTEND-B: a preview response that is missing
// (or sends null / a wrong-typed value for) a list field must NOT throw
// downstream. The original prod crash was `res.to_add.filter(...)` on an
// undefined field; the rewrite reads `res.entries`, so we lock the boundary.
describe("normalizePreviewResponse", () => {
  // Each "malformed" response a misbehaving backend might send.
  const malformed: Array<[string, unknown]> = [
    ["undefined", undefined],
    ["null", null],
    ["empty object", {}],
    ["missing entries", { parse_errors: [], existing_ids: [] }],
    ["null fields", { entries: null, parse_errors: null, existing_ids: null }],
    ["wrong-typed fields", { entries: "nope", parse_errors: 7, existing_ids: {} }],
  ];

  for (const [label, raw] of malformed) {
    it(`coerces every field to an array for ${label}`, () => {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const res = normalizePreviewResponse(raw as any);
      expect(Array.isArray(res.entries)).toBe(true);
      expect(Array.isArray(res.parse_errors)).toBe(true);
      expect(Array.isArray(res.existing_ids)).toBe(true);
      // The downstream consumers must not throw on the normalized shape.
      expect(() => {
        groupEntries(res.entries);
        defaultSelectedKeys(res.entries);
        defaultIds(res.entries);
        void res.entries.length;
        void res.parse_errors.length;
      }).not.toThrow();
    });
  }

  it("passes a well-formed response through unchanged", () => {
    const good = makeEntry({ original_id: "a", proposed_id: "web-a" });
    const res = normalizePreviewResponse({
      entries: [good],
      parse_errors: ["oops"],
      existing_ids: ["x"],
    });
    expect(res.entries).toEqual([good]);
    expect(res.parse_errors).toEqual(["oops"]);
    expect(res.existing_ids).toEqual(["x"]);
  });
});

describe("normalizeResultResponse", () => {
  it("coerces missing / null / wrong-typed fields to arrays", () => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const res = normalizeResultResponse({ added: null, errors: "x" } as any);
    expect(res.added).toEqual([]);
    expect(res.skipped).toEqual([]);
    expect(res.errors).toEqual([]);
  });
});
