from __future__ import annotations

import ipaddress
import os
from pathlib import Path
from typing import Any, Literal, Self, cast

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Mode = Literal["standalone", "cloud"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MCPOLIS_", env_file=".env", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 8080

    # Deployment mode. ``standalone`` is the self-hosted single-org
    # experience backed by JSON files; ``cloud`` (wired up in Phase 2c)
    # is multi-tenant and Mongo/Redis-backed. Phase 2b only introduces
    # the env var + validation scaffolding; the cloud code paths land
    # in 2c.
    mode: Mode = Field(default="standalone")

    # Google OAuth settings
    google_client_id: str = Field(default="")
    google_client_secret: str = Field(default="")
    server_url: str = Field(default="http://localhost:8000")

    # Public base URL the gateway MCP is exposed at. Most deploys leave
    # this empty so the gateway and dashboard share an origin (the
    # value of ``server_url``). Set it explicitly when the gateway
    # lives on its own subdomain, e.g. ``MCPOLIS_GATEWAY_URL=
    # https://mcp.mcphero.io`` while ``MCPOLIS_SERVER_URL=
    # https://mcphero.io`` keeps the dashboard on apex. Resolve via
    # ``effective_gateway_url(settings)`` — empty falls back to
    # ``server_url`` so dev / single-origin deploys are unaffected.
    gateway_url: str = Field(default="")

    # Dashboard browser-login provider — single switch for the cookie
    # path. Defaults flip per mode (see ``_apply_mode_defaults``):
    # standalone → ``dev_stub``, cloud → ``google``.
    #
    # Values:
    #   "google"   — Google sign-in (production). Requires
    #                ``google_client_id`` / ``google_client_secret``.
    #   "dev_stub" — In-app email picker that issues a real signed
    #                cookie without any external round-trip. Refused at
    #                startup if ``mode == "cloud"``.
    oauth_provider: Literal["google", "dev_stub"] = Field(default="dev_stub")

    mcp_json_path: Path = Field(default=Path("config/mcp.json"))
    config_path: Path = Field(default=Path("config/config.json"))
    oauth_apps_path: Path = Field(default=Path("config/oauth_apps.json"))
    # Inline JSON alternative to ``oauth_apps_path`` — when non-empty,
    # credentials are parsed from this string instead of a file on disk.
    # Useful for Docker / CI deployments that ship secrets via env vars
    # (``MCPOLIS_OAUTH_APPS_JSON``) rather than a mounted file.
    oauth_apps_json: str = Field(default="")
    data_dir: Path = Field(default=Path("data"))
    audit_log_path: Path = Field(default=Path("data/audit.jsonl"))
    # Bumped from 7 to 365 to cover the longest plan retention
    # (``Team``). Per-org caps are applied at read time in the audit
    # search route — the global TTL is just the floor that physically
    # purges rows.
    audit_retention_days: int = Field(default=365)

    frontend_dist: Path | None = Field(default=None)

    # Feature flags
    # Stdio MCP support. Defaults to True in standalone (self-hosters
    # expect to run local command-line MCP servers) and False in cloud
    # (no local processes in a multi-tenant deployment). Setting
    # ``MCPOLIS_ALLOW_STDIO_MCP`` explicitly overrides this for either
    # mode — useful for cloud dev installs that want to smoke-test
    # stdio handling.
    allow_stdio_mcp: bool = Field(default=True)

    # Dashboard session signing key. Required in cloud mode; optional in
    # standalone mode (falls back to ``google_client_secret`` → dev default).
    session_secret: str = Field(default="")

    # Cloud-mode secrets (Phase 2c wires these into real implementations).
    # Declared here so the validation scaffolding below can surface a clear
    # error if they're missing.
    encryption_key: str = Field(default="")
    mongo_uri: str = Field(default="")
    mongo_db_name: str = Field(default="mcpolis")
    redis_url: str = Field(default="")

    # Instance-level superadmin email allowlist (cloud mode only).
    # Comma-separated list of emails that can access /admin-mcp/system.
    superadmin_emails: str = Field(default="")

    # Rate limits (per-minute). Applied by the Phase 2d
    # ``RateLimitMiddleware`` in both modes — in-memory counter in
    # standalone, Redis sliding-window ZSET in cloud.
    rate_limit_auth_per_min: int = Field(default=10)
    rate_limit_tool_per_min: int = Field(default=100)
    rate_limit_admin_per_min: int = Field(default=50)

    # Graceful drain timeout in seconds. On SIGTERM the backend stops
    # accepting new requests and waits this long for in-flight ones.
    drain_timeout: float = Field(default=30.0)

    # PolicyNotifier debounce window in seconds. Rapid admin edits
    # collapse to one ``tools/list_changed`` push per burst; this sets
    # the "end of burst" threshold. Default is tuned for UI admins
    # clicking toggles quickly; E2E tests override to sub-second so
    # they don't have to wait out a real burst window.
    policy_notifier_debounce_seconds: float = Field(default=10.0)

    # Sentry. Empty DSN disables Sentry entirely — safe for dev and
    # for self-hosters who don't want error reporting.
    sentry_dsn: str = Field(default="")
    sentry_environment: str = Field(default="")
    sentry_traces_sample_rate: float = Field(default=0.1)
    release: str = Field(default="")

    # Mixpanel product analytics. Empty token disables analytics entirely.
    mixpanel_token: str = Field(default="")
    # Host override for EU data residency (e.g. ``api-eu.mixpanel.com``).
    # Empty uses the US default baked into the SDK.
    mixpanel_api_host: str = Field(default="")

    # §5.2 upstream-health email notifications.
    # Defaults to False so the pipeline ships behind a flag: with
    # ``MCPOLIS_UPSTREAM_HEALTH_EMAIL_ENABLED=false`` the notifier still
    # runs and logs every message via ``StubEmailSender`` (useful for
    # smoke-testing the decision logic), but nothing leaves the process.
    # Flip to True once a real ``EmailSender`` adapter is wired in.
    upstream_health_email_enabled: bool = Field(default=False)

    # SMTP transport for the §5.2 notifier (and any future
    # transactional mail). Empty ``smtp_host`` ⇒ fall back to the
    # logging ``StubEmailSender`` (dev / standalone default); a set
    # host wires in the real ``SmtpEmailSender``.
    #
    # Google Workspace submission: smtp.gmail.com:587 (STARTTLS),
    # authenticate as a real Workspace user with an App Password
    # (requires 2-Step Verification on that account). ``smtp_from`` is
    # the visible From address — ``info@mcphero.io`` as a verified
    # "Send mail as" alias on the auth user — so ``smtp_username`` and
    # ``smtp_from`` deliberately differ. Port 465 selects implicit TLS;
    # any other port (587) uses STARTTLS.
    smtp_host: str = Field(default="")
    smtp_port: int = Field(default=587)
    smtp_username: str = Field(default="")
    smtp_password: str = Field(default="")
    smtp_from: str = Field(default="")
    smtp_from_name: str = Field(default="MCP Hero")

    # Enables the ``/api/auth/test-mcp-token``
    # endpoints, which mint sessions / bearer tokens for arbitrary emails
    # without going through Google OAuth. E2E-only: the cloud validator
    # rejects startup if this is true in cloud mode. Read once at startup
    # — do NOT re-read os.environ per request.
    test_mode: bool = Field(default=False)

    # Bundled "kitchen sink" demo MCP server (mcpolis.dev.demo_mcp_server).
    # Two independent toggles:
    #
    # - ``MCPOLIS_DEMO_MOUNT`` mounts the demo at ``/dev/mcp-demo`` so any
    #   client (including prod smoke tests) can hit it as an HTTP upstream
    #   target. The mount has no auth — same origin / process — so don't
    #   enable in untrusted environments.
    # - ``MCPOLIS_DEMO_SEED`` auto-registers the demo as a
    #   service-account upstream on the default org (standalone mode) or
    #   the first org (cloud). Cloud-mode seeding lands the upstream on
    #   whichever real customer org happens to be first, so this defaults
    #   off and should stay off in prod; the standalone dev stack flips
    #   it on so the local dashboard ships with a working demo upstream.
    #
    # Both off in prod by default. ``bash start.sh`` flips both on for dev;
    # ``--no-demo`` clears both. Field names align with env vars so
    # pydantic-settings maps ``MCPOLIS_DEMO_MOUNT`` → ``demo_mount`` and
    # ``MCPOLIS_DEMO_SEED`` → ``demo_seed``.
    demo_mount: bool = Field(default=False)
    demo_seed: bool = Field(default=False)
    # Stable upstream id under which the demo gets registered when seeded.
    demo_upstream_id: str = Field(default="mcp-demo")
    # Test-fixture stdio MCP. When enabled, the backend serves
    # :file:`backend/src/mcpolis/dev/stdio_test_mcp_server.py` as
    # plain text at ``GET /dev/stdio-test-mcp.py`` so the integration
    # / E2E suites can ``uv run <https-url>`` it inside an E2B
    # sandbox. Off by default; flip via ``MCPOLIS_STDIO_TEST_MCP=1``
    # or ``bash start.sh --with-stdio-test``. Auto-registration is
    # NOT done — it's a fixture, not a demo, and the test harness
    # adds the upstream explicitly via the create-upstream API.
    stdio_test_mcp: bool = Field(default=False)

    # ----------------------------------------------------------------
    # Sandbox provider selection.
    # ----------------------------------------------------------------
    # Picks which SandboxService backend handles every stdio MCP. One
    # of: ``e2b``, ``local-subprocess``. Empty ⇔ ``e2b`` when an API
    # key is configured, else ``local-subprocess`` (the unsafe dev
    # path with a startup warning). The own-runner backend was deleted
    # in plan ``serene-beaming-tulip.md`` §Phase 5; rejecting that
    # value at startup keeps stale env files honest.
    sandbox_provider: str = Field(default="")
    # E2B API key. Required (and validated at startup) only when
    # ``sandbox_provider=e2b`` in cloud mode. Standalone runs may set
    # the value but it's never enforced.
    e2b_api_key: str = Field(default="")
    # Operator switch for E2B Volumes (the persistent-disk feature
    # behind ``UpstreamDefinition.stdio.persistent_disk_enabled``).
    # The Volumes API exists in the SDK but is gated by an account-
    # level feature flag — calls return ``403: use of volumes is not
    # enabled`` until enabled in the E2B dashboard. Defaulting OFF
    # means a fresh deploy doesn't expose a UI toggle that breaks on
    # first use; flip to ``true`` once the operator has confirmed the
    # E2B account supports volumes.
    e2b_volumes_enabled: bool = Field(default=False)
    # E2B-side idle window before a sandbox auto-pauses. Passed
    # through to ``Sandbox.create(timeout=…)`` together with
    # ``lifecycle={on_timeout: pause, auto_resume: True}``. After this
    # many seconds without an API call, the sandbox snapshots; the
    # next call (typically a tool invocation) auto-resumes it and
    # ``E2BSandboxService._session_cm`` reattaches the streaming RPC.
    # Default 60s — paused sandboxes are free (E2B billing docs:
    # "Once a sandbox is paused, killed or times out, billing stops
    # immediately"), wake-from-paused was empirically measured at
    # 361–759 ms in the integration suite, so a tighter window is
    # the right cost/UX trade-off. Lower bound: ~60s; below that,
    # sandboxes risk pausing mid-operation.
    e2b_idle_pause_seconds: int = Field(default=60)
    # Reuse-on-restart for stdio service_account upstreams. When
    # ``True``, ``E2BSandboxService`` writes a live ``(sandbox_id,
    # pid)`` ref on session entry; the lifespan handler marks
    # every active session preserve-on-close before graceful
    # shutdown so sandboxes outlive the mcpolis process. The next
    # boot's ``_try_reconnect`` picks them up via ``Sandbox.connect``
    # + ``commands.connect(pid)``, turning the ~25 s deploy
    # cold-install into a ~700 ms wake-from-paused. Reattach is
    # unconditional: config edits on disk do not propagate until
    # the user explicitly Stop+Restart the upstream (which kills
    # the sandbox so the next start fresh-creates with the new
    # config). Defaults ``True`` because the cost/UX argument is
    # unambiguous (paused sandboxes are free; wake is sub-second).
    # Set to ``False`` to revert to clean-slate-per-deploy
    # semantics.
    e2b_reuse_sandboxes_on_restart: bool = Field(default=True)
    # One-shot operator override: when ``True``, the next boot kills
    # every persisted sandbox + clears every ref + does fresh
    # creates from scratch. Useful for "I think the MCP got into a
    # bad state, give me a clean slate" without disabling reuse
    # permanently. Set this for a single restart, then unset.
    e2b_fresh_sandboxes: bool = Field(default=False)

    @model_validator(mode="before")
    @classmethod
    def _coerce_empty_bool_env_to_false(cls, data: Any) -> Any:
        # Pydantic's strict bool parser rejects ``""`` outright, which
        # made a stray ``MCPOLIS_DEMO_SEED=`` line crash prod startup
        # mid-secrets-rotation. Operators reasonably read an empty
        # value as "declared but off"; honour that for every bool
        # field rather than re-litigating per future toggle.
        if not isinstance(data, dict):
            return data
        typed_data = cast(dict[str, Any], data)
        for name, info in cls.model_fields.items():
            if info.annotation is bool and typed_data.get(name) == "":
                typed_data[name] = False
        return typed_data

    @model_validator(mode="after")
    def _apply_mode_defaults(self) -> Self:
        # Flip stdio default to False in cloud mode when the env var
        # wasn't explicitly set. ``model_fields_set`` only contains
        # fields provided via env / constructor — not defaults.
        if (
            "allow_stdio_mcp" not in self.model_fields_set
            and self.mode == "cloud"
        ):
            self.allow_stdio_mcp = False
        # Cloud-mode default for the OAuth provider is google. The field
        # default is ``dev_stub`` (the standalone-friendly choice); flip
        # only when the field wasn't explicitly set.
        if (
            "oauth_provider" not in self.model_fields_set
            and self.mode == "cloud"
        ):
            self.oauth_provider = "google"
        return self

    def parsed_superadmin_emails(self) -> set[str]:
        """Parse ``superadmin_emails`` (comma-separated) into a set.

        Single source of truth for the instance-level superadmin
        allowlist — used by the dashboard auth dependencies, the
        cross-org dashboard / debug / system-MCP routers, and the
        org-context middleware's super-admin org override. Empty in
        standalone mode (the feature is cloud-only).
        """
        return {
            e.strip()
            for e in self.superadmin_emails.split(",")
            if e.strip()
        }


class StartupConfigError(RuntimeError):
    """Raised at startup when required secrets are missing or insecure."""


def effective_dashboard_provider(settings: Settings) -> str:
    """Resolve the dashboard OAuth provider name actually in effect.

    Thin wrapper now that ``Settings.oauth_provider`` is mandatory and
    mode-defaulted; kept as a function so callers can keep importing
    one symbol if the resolution rule grows again later.
    """
    return settings.oauth_provider


def effective_gateway_url(settings: Settings) -> str:
    """Resolve the public base URL of the gateway MCP.

    Returns ``settings.gateway_url`` when set, otherwise falls back to
    ``settings.server_url``. Most deploys keep the dashboard and the
    gateway on the same origin (so ``gateway_url`` is empty); set it
    explicitly when running the gateway on its own subdomain.

    The trailing slash is stripped so callers can build paths with a
    plain ``f"{base}/mcp"`` regardless of how the operator wrote the
    env var.
    """
    base = settings.gateway_url or settings.server_url
    return base.rstrip("/")


def _is_loopback_bind(host: str) -> bool:
    """True only for literal loopback IP addresses (127.0.0.0/8, ::1).

    Hostnames such as ``localhost`` resolve via ``/etc/hosts`` and are
    rejected on purpose — the guard needs to be unambiguous, so callers
    who want to allow cloud+test_mode must set ``MCPOLIS_HOST`` to a
    literal loopback address.
    """
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_startup_secrets(settings: Settings) -> None:
    """Fail fast if the running mode is missing required secrets.

    Standalone mode accepts dev defaults (nothing to validate here — the
    app is running on the user's own machine). Cloud mode refuses to start
    without ``SESSION_SECRET``, ``ENCRYPTION_KEY``, ``MONGO_URI`` and
    ``REDIS_URL``; the latter three aren't consumed yet (Phase 2c), but
    we surface the requirement now so a misconfigured cloud deploy fails
    at startup rather than hours later on the first write.
    """
    # Defense-in-depth: the dev-stub provider issues real session cookies
    # without any identity check. It's intended for local development
    # and e2e tests; refusing it in cloud mode at startup means a
    # misapplied env var can't silently turn a production deployment
    # into an open-door auth surface.
    #
    # The exception mirrors the ``test_mode`` escape hatch below:
    # cloud + dev_stub is acceptable when ``MCPOLIS_TEST_MODE=1`` AND
    # the bind is a literal loopback address. ``run-e2e-tests.sh``
    # relies on this combination to exercise cloud code paths
    # (Mongo + Redis storage, multi-org routing) without needing real
    # Google credentials. A misapplied production deploy would have
    # ``test_mode=False`` or be bound off-loopback and still be
    # rejected.
    if settings.oauth_provider == "dev_stub" and settings.mode == "cloud":
        if not (settings.test_mode and _is_loopback_bind(settings.host)):
            raise StartupConfigError(
                "MCPOLIS_OAUTH_PROVIDER=dev_stub is not allowed in cloud mode. "
                "Use MCPOLIS_OAUTH_PROVIDER=google in cloud deployments. "
                "(Local cloud-mode e2e runs may set MCPOLIS_TEST_MODE=1 with "
                "a loopback MCPOLIS_HOST to exercise this path.)"
            )

    if settings.mode == "standalone":
        return

    # ``test_mode`` exposes ``/api/auth/test-mcp-token`` (gateway bearer
    # token minter) for arbitrary emails. In cloud mode the combination
    # is only acceptable when bound to a loopback address, so even a
    # misapplied deploy can't serve that route off-host. This is the
    # escape hatch ``run-e2e-tests.sh`` relies on to exercise cloud
    # code paths locally.
    if settings.test_mode and not _is_loopback_bind(settings.host):
        raise StartupConfigError(
            "MCPOLIS_TEST_MODE is enabled in cloud mode but "
            f"MCPOLIS_HOST={settings.host!r} is not a loopback address. "
            "The test-mcp-token endpoint mints unauthenticated bearer "
            "tokens for arbitrary emails; in cloud mode it may only be "
            "exposed on 127.0.0.1 or ::1."
        )

    if settings.oauth_provider == "google" and not settings.google_client_id:
        raise StartupConfigError(
            "MCPOLIS_GOOGLE_CLIENT_ID is required when "
            "MCPOLIS_OAUTH_PROVIDER=google."
        )

    # SSRF deny-list (security finding F-01) blocks user-supplied
    # upstream URLs that point at private/loopback ranges. The
    # ``MCPOLIS_TEST_SAFE_HTTP_ALLOW_LOOPBACK=1`` flag opens up
    # 127.0.0.0/8 + ``::1`` so dev/e2e fixtures (demo MCP, fake OAuth
    # server) keep working. In cloud mode the flag is only acceptable
    # when bound to a loopback address — a real prod deploy listens on
    # ``0.0.0.0`` (or a public IP), so the flag would let a co-tenant
    # admin probe the EC2 host's local services.
    if (
        os.environ.get("MCPOLIS_TEST_SAFE_HTTP_ALLOW_LOOPBACK") == "1"
        and not _is_loopback_bind(settings.host)
    ):
        raise StartupConfigError(
            "MCPOLIS_TEST_SAFE_HTTP_ALLOW_LOOPBACK=1 is only permitted "
            f"with a loopback MCPOLIS_HOST. Got {settings.host!r}; a "
            "non-loopback bind is treated as production and must keep "
            "the strict SSRF deny-list intact."
        )

    missing: list[str] = []
    if not settings.session_secret or settings.session_secret == "mcpolis-dev-secret":
        missing.append("MCPOLIS_SESSION_SECRET")
    if not settings.encryption_key:
        missing.append("MCPOLIS_ENCRYPTION_KEY")
    if not settings.mongo_uri:
        missing.append("MCPOLIS_MONGO_URI")
    if not settings.redis_url:
        missing.append("MCPOLIS_REDIS_URL")

    if missing:
        raise StartupConfigError(
            "Cloud mode requires the following env vars to be set to "
            "non-dev values: " + ", ".join(missing)
        )

    # Sandbox provider: cloud mode allows only ``e2b``. The
    # ``local-subprocess`` no-isolation path is dev-only; the
    # ``own-runner`` backend was deleted in Phase 5 and is rejected
    # outright so a stale env var doesn't silently disable the
    # sandbox.
    if settings.sandbox_provider:
        if settings.sandbox_provider == "own-runner":
            raise StartupConfigError(
                "MCPOLIS_SANDBOX_PROVIDER=own-runner is no longer supported. "
                "The own-runner backend was deleted; remove the env var or "
                "set it to 'e2b'."
            )
        if settings.sandbox_provider not in {"e2b", "local-subprocess"}:
            raise StartupConfigError(
                "MCPOLIS_SANDBOX_PROVIDER must be one of "
                "'e2b' / 'local-subprocess'; got "
                f"{settings.sandbox_provider!r}."
            )
        if settings.sandbox_provider == "e2b" and not settings.e2b_api_key:
            raise StartupConfigError(
                "MCPOLIS_SANDBOX_PROVIDER=e2b requires "
                "MCPOLIS_E2B_API_KEY to be set so the SDK can authenticate."
            )
        if settings.sandbox_provider == "local-subprocess":
            raise StartupConfigError(
                "MCPOLIS_SANDBOX_PROVIDER=local-subprocess is not allowed "
                "in cloud mode — it runs every stdio MCP unsandboxed on "
                "the backend host."
            )

    # §5.2 email notifications. Flipping the flag on in cloud mode
    # without a configured SMTP transport would silently "send" via
    # the logging stub — mail never reaches admins. Fail fast so the
    # operator wires SMTP (host / username / password / from) before
    # enabling, rather than discovering the no-op in production.
    if settings.upstream_health_email_enabled:
        smtp_missing = [
            name
            for name, value in (
                ("MCPOLIS_SMTP_HOST", settings.smtp_host),
                ("MCPOLIS_SMTP_USERNAME", settings.smtp_username),
                ("MCPOLIS_SMTP_PASSWORD", settings.smtp_password),
                ("MCPOLIS_SMTP_FROM", settings.smtp_from),
            )
            if not value
        ]
        if smtp_missing:
            raise StartupConfigError(
                "MCPOLIS_UPSTREAM_HEALTH_EMAIL_ENABLED=true requires a "
                "configured SMTP transport; missing: "
                + ", ".join(smtp_missing)
            )

