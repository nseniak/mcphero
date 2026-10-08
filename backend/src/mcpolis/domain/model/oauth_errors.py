"""OAuth error codes with a fixed meaning across the sign-in services."""
from __future__ import annotations


# OAuth error codes the upstream returns to mean "this is permanently
# rejected, retrying won't help": the refresh token is dead
# (``invalid_grant``) or the client registration is dead
# (``invalid_client`` — expired / rotated / forgotten DCR). Both are
# terminal: delete the token immediately and (via
# ``purge_user_oauth_state``) drop the DCR client so the next consent
# re-registers, rather than churning the same rejection every tick.
# The §5.2 warning email reads the same list, so every sign-in deleted
# for a rejection gets its warning.
TERMINAL_AUTH_ERROR_CODES: tuple[str, ...] = ("invalid_grant", "invalid_client")
