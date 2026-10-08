/**
 * Only ASCII letter case and ASCII white space at the ends carry no
 * meaning in an address, as in the backend's ``same_email``. The cases
 * live in ``email-address-cases.json``, which the backend's
 * ``test_email_address.py`` runs too, so the two rules can't drift.
 */
import { describe, expect, it } from "vitest";

import casesText from "./email-address-cases.json?raw";
import { emailKey, sameEmail } from "./email-address";

interface EmailCases {
  same: [string, string][];
  different: [string, string][];
}

const cases = JSON.parse(casesText) as EmailCases;

describe("sameEmail", () => {
  it.each(cases.same)("calls %j and %j one person", (a, b) => {
    expect(sameEmail(a, b)).toBe(true);
  });

  it.each(cases.different)("calls %j and %j two people", (a, b) => {
    expect(sameEmail(a, b)).toBe(false);
  });
});

describe("emailKey", () => {
  it("lower-cases A-Z only and trims ASCII white space", () => {
    expect(emailKey(" \tBOB.Smith@Acme.COM\n")).toBe("bob.smith@acme.com");
    expect(emailKey("Kate@Über.de")).toBe("Kate@Über.de");
  });
});
