/**
 * Comparing email addresses.
 *
 * The letter case of an address carries no meaning: an admin may invite
 * ``Bob@Acme.com`` while Bob signs in as ``bob@acme.com``, and the Team
 * page lists him as the admin typed him. Code asking "is this the same
 * person?" uses ``sameEmail``.
 *
 * Only ASCII letter case is ignored: ``A``-``Z`` become ``a``-``z`` and no
 * other character changes, and only ASCII white space is trimmed.
 * ``toLowerCase`` would also lower-case non-ASCII letters (the Kelvin sign
 * U+212A becomes ``k``), so two different mailboxes would compare equal.
 *
 * The same rule as ``email_key`` / ``same_email`` in
 * ``backend/src/mcpolis/domain/model/email_address.py``. The cases in
 * ``email-address-cases.json`` run against both, so the two can't drift.
 */

// White space ``emailKey`` trims from both ends: ASCII only, the same set
// as the backend's rule.
const ASCII_WHITESPACE_AT_ENDS = /^[ \t\n\r\v\f]+|[ \t\n\r\v\f]+$/g;
const ASCII_CAPITAL = /[A-Z]/g;

/** The form of ``email`` two addresses are compared in. */
export function emailKey(email: string): string {
  return email
    .replace(ASCII_WHITESPACE_AT_ENDS, "")
    .replace(ASCII_CAPITAL, (letter) => letter.toLowerCase());
}

export function sameEmail(a: string, b: string): boolean {
  return emailKey(a) === emailKey(b);
}
