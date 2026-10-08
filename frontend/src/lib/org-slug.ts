/**
 * Org short-name ("slug") helpers shared by the Signup and
 * Organizations pages. Mirrors the backend rule in
 * ``domain/services/org_service.py`` (``MAX_SLUG_LENGTH``,
 * ``_SLUG_PATTERN``, ``suggest_slug``) so the form never accepts a
 * name the server will refuse.
 */

/** Must match ``MAX_SLUG_LENGTH`` in ``org_service.py``. */
export const MAX_SLUG_LENGTH = 20;
const MIN_SLUG_LENGTH = 3;

const SLUG_PATTERN = new RegExp(
  `^[a-z0-9][a-z0-9-]{1,${MAX_SLUG_LENGTH - 2}}[a-z0-9]$`,
);

/** Derive a slug from a display name (backend ``suggest_slug``,
 * minus its ``"org"`` fallback: an empty name leaves the field empty). */
export function suggestSlug(name: string): string {
  return name
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, MAX_SLUG_LENGTH)
    .replace(/-+$/, "");
}

/** Clean what the user types into the slug field: lowercase, and
 * drop every character a slug can't hold. */
export function sanitizeSlugInput(raw: string): string {
  return raw.toLowerCase().replace(/[^a-z0-9-]/g, "");
}

/** Return a user-facing error for a malformed slug, or ``null``.
 * Empty input is not an error (the field just hasn't been filled). */
export function validateSlug(value: string): string | null {
  if (!value) return null;
  if (/[^a-z0-9-]/.test(value)) return "Only lowercase letters, numbers, and hyphens allowed";
  if (value.startsWith("-") || value.endsWith("-")) return "Cannot start or end with a hyphen";
  if (value.length < MIN_SLUG_LENGTH) return `Must be at least ${MIN_SLUG_LENGTH} characters`;
  if (value.length > MAX_SLUG_LENGTH) return `Must be at most ${MAX_SLUG_LENGTH} characters`;
  if (!SLUG_PATTERN.test(value)) return "Invalid format";
  return null;
}
