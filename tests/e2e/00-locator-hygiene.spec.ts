/**
 * Meta-test: org-row locator hygiene.
 *
 * Each shard runs many specs against ONE shared backend/org, so the
 * superadmin orgs list and /orgs/manage accumulate extra orgs that
 * co-located specs created. Picking an org row with ``.first()`` on a
 * per-org testid then silently targets the wrong org — which surfaced as
 * deterministic "Expected team, Received free" failures in 34-plan-gates
 * that only appeared under certain bin-packer shard packings. The fix is
 * to scope to the org's own row (filter the row by its slug). This guard
 * keeps the antipattern from creeping back. Pure file scan — no stack.
 */
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

import { test, expect } from "@playwright/test";

// testids that render once PER ORG ROW. ``.first()`` on these picks an
// arbitrary org row, not necessarily the spec's own org.
const PER_ORG_ROW_TESTIDS = ["plan-badge", "superadmin-plan-select"];

const SELF = "00-locator-hygiene.spec.ts";

test("e2e specs never select a per-org row with .first()", () => {
  // ``\s*`` spans newlines, so a ``.first()`` on the following line is
  // caught too (how 34-plan-gates test 9 was originally written).
  const pattern = new RegExp(
    String.raw`getByTestId\(\s*["'](?:` +
      PER_ORG_ROW_TESTIDS.join("|") +
      String.raw`)["']\s*\)\s*\.first\(`,
  );
  const offenders = readdirSync(__dirname)
    .filter((f) => f.endsWith(".spec.ts") && f !== SELF)
    .filter((f) => pattern.test(readFileSync(join(__dirname, f), "utf8")));

  expect(
    offenders,
    `These specs select a per-org row with .first() on one of ` +
      `[${PER_ORG_ROW_TESTIDS.join(", ")}]. Under sharded runs that can ` +
      `target the wrong org (co-located specs leave extra orgs in the ` +
      `list). Scope to the org's own row instead — e.g. ` +
      `page.locator("tbody tr").filter({ has: page.getByText(ORG, ` +
      `{ exact: true }) }).getByTestId(...). Offenders: ` +
      `${offenders.join(", ")}`,
  ).toEqual([]);
});
