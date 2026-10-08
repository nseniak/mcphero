/**
 * Unit tests for the shared org short-name helpers. The length cap
 * must match the backend's ``MAX_SLUG_LENGTH`` (20): both pages used
 * to accept 40, so the server refused names the form called valid.
 */
import { describe, expect, it } from "vitest";

import {
  MAX_SLUG_LENGTH,
  sanitizeSlugInput,
  suggestSlug,
  validateSlug,
} from "./org-slug";

describe("suggestSlug", () => {
  it("dashes a free-form display name", () => {
    expect(suggestSlug("  Acme Corp, Inc. ")).toBe("acme-corp-inc");
  });

  it("caps at the backend length and never ends on a hyphen", () => {
    const slug = suggestSlug("abcdefghijklmnopqrs tuvwxyz");
    expect(slug).toBe("abcdefghijklmnopqrs");
    expect(slug.length).toBeLessThanOrEqual(MAX_SLUG_LENGTH);
  });

  it("leaves an empty name empty", () => {
    expect(suggestSlug("!!!")).toBe("");
  });
});

describe("sanitizeSlugInput", () => {
  it("lowercases and drops disallowed characters", () => {
    expect(sanitizeSlugInput("Ac_me Co!")).toBe("acmeco");
  });
});

describe("validateSlug", () => {
  it("accepts a well-formed slug at the length cap", () => {
    expect(validateSlug("a".repeat(MAX_SLUG_LENGTH))).toBeNull();
  });

  it("rejects a slug longer than the backend allows", () => {
    expect(validateSlug("a".repeat(MAX_SLUG_LENGTH + 1))).toBe(
      "Must be at most 20 characters",
    );
  });

  it("gives the same answer when called twice in a row", () => {
    // The old copies tested with a ``/g`` regex, whose ``lastIndex``
    // made every second call on the same bad input pass.
    expect(validateSlug("ab_c")).not.toBeNull();
    expect(validateSlug("ab_c")).not.toBeNull();
  });

  it("explains each rule", () => {
    expect(validateSlug("")).toBeNull();
    expect(validateSlug("-abc")).toBe("Cannot start or end with a hyphen");
    expect(validateSlug("ab")).toBe("Must be at least 3 characters");
  });
});
