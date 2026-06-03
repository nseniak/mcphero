import { test, expect } from "@playwright/test";

const ORG = "acme-corp";
// JoinPage hydrates the heading + body copy with the org's
// ``display_name`` from /api/orgs/{slug}/public, falling back to the
// raw slug only while that fetch is in flight or the slug doesn't
// resolve. The acme seed org's display name is "Acme Corp".
const DISPLAY_NAME = "Acme Corp";

test.describe("Join Flow", () => {
  test("join page renders for unauthenticated user", async ({ page }) => {
    await page.goto(`/orgs/${ORG}/join`);
    // Heading is "Join <span>Acme Corp</span>" — accessible name is
    // the concatenation, so match by role to avoid the span split.
    await expect(
      page.getByRole("heading", { name: `Join ${DISPLAY_NAME}` })
    ).toBeVisible();
    await expect(
      page.getByRole("link", { name: /sign in with google/i })
    ).toBeVisible();
  });

  test("join page shows error for non-member", async ({ page }) => {
    // Simulate the redirect from the backend with auth_error params
    await page.goto(
      `/orgs/${ORG}/join?auth_error=not_a_member&email=stranger@test.com&org=${ORG}`
    );
    await expect(page.getByText("stranger@test.com")).toBeVisible();
    await expect(
      page.getByText(/isn't on the .* team yet/)
    ).toBeVisible();
    await expect(
      page.getByRole("link", { name: /try a different account/i })
    ).toBeVisible();
  });

  test("join link includes org slug in login URL", async ({ page }) => {
    await page.goto(`/orgs/${ORG}/join`);
    const signInLink = page.getByRole("link", { name: /sign in with google/i });
    const href = await signInLink.getAttribute("href");
    expect(href).toContain(`join=${ORG}`);
  });
});
