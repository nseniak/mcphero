# Team

Everything you do on the **Team** page: inviting members, changing their roles, and removing them.

If "role" or "team member" are unfamiliar, see [Concepts](concepts.md).

## Inviting a teammate

Two steps — add them, then send them the invite link.

1. Open the **Team** page and click **Add Member**.
2. Enter the teammate's **Google email**. (MCP Hero authenticates everyone via Google sign-in, so the email has to be one that resolves to a Google account — a personal Gmail or a Google Workspace email.)
3. Pick a **Role**. The role decides which MCPs and tools the member can access — see [Roles and permissions](roles-and-permissions.md) for what each role allows.
4. Click **Add**. The member appears in the list with status **Pending**.
5. Copy the invite link at the top of the page (under *Invite team members*) and send it to the teammate.

When the teammate clicks the link and signs in with Google, they land directly in your organization with the role you assigned, and their status flips to **Joined**.

> **The invite link is per-organization, not per-member**
>
> Anyone *you've already added* can sign in through the same link — it's just a shortcut to your org's sign-in. Someone you haven't added can't use it to join. There's no per-member invite token to keep secret.

## Changing a member's role

1. Open the **Team** page and click the member's email in the list. You land on their detail page.
2. Click **Edit** in the **Settings** section. The role becomes a dropdown.
3. Pick the new role and click **Save**.

The change takes effect immediately — the member's MCP and tool access updates on their next request.

You can't edit your own role from this page (the **Edit** button doesn't appear when looking at yourself). Ask another admin to change your role, or see [Roles and permissions](roles-and-permissions.md) for managing the role itself.

## Removing a member

1. Open the **Team** page and click the member's email.
2. Click **Edit** in the **Settings** section.
3. Click **Remove** at the bottom of the section. Confirm in the dialog.

Removing a member:

- Revokes their gateway tokens (their AI client stops getting tools immediately)
- Disconnects any per-user upstream sessions they had open (per-user OAuth tokens are dropped)
- Removes them from your organization

You can't remove yourself. To leave an organization, ask another admin to remove you.
