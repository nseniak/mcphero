/**
 * URL-safe slug generation for Sandbox-file storage keys.
 *
 * The backend's ``name`` field is a path component on
 * ``PUT /api/admin/upstreams/{id}/sandbox-files/{name}``, so it
 * must match a strict URL-safe grammar. The Files modal lets the
 * operator type a free-form ``display_name``; this helper derives
 * the storage key from that label so they don't have to think
 * about URL-safety.
 *
 * Mirrors ``slugify_sandbox_file_name`` in
 * ``backend/src/mcpolis/domain/model/sandbox_file.py`` so the
 * frontend's preview matches what the backend would persist.
 */

const SLUG_MAX_LEN = 64;
const ALLOWED_RUN_RE = /[^a-z0-9._-]+/g;

export function slugifySandboxFileName(raw: string): string {
  const lowered = raw.trim().toLowerCase();
  if (!lowered) return "";
  let slug = lowered.replace(ALLOWED_RUN_RE, "-");
  // Strip leading / trailing dashes.
  slug = slug.replace(/^-+/, "").replace(/-+$/, "");
  if (slug.length > SLUG_MAX_LEN) {
    slug = slug.slice(0, SLUG_MAX_LEN).replace(/-+$/, "");
  }
  return slug;
}

const VALID_NAME_RE = /^[A-Za-z0-9._-]+$/;

export function isValidSandboxFileName(name: string): boolean {
  return (
    name.length > 0
    && name.length <= SLUG_MAX_LEN
    && VALID_NAME_RE.test(name)
  );
}
