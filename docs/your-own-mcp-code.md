# Running your own MCP code

You've written your own MCP server and want your team to use it through MCP Hero. The sandbox can run any command you point it at, but the bytes have to reach the sandbox somehow. This page covers the two stdio distribution patterns (public package, private package with a registry credential) plus the HTTP-MCP escape hatch when neither fits.

If "stdio MCP", "Variables", or "Files" don't ring a bell, [Concepts](concepts.md) and [Upstream MCPs](upstream-mcps.md) are better entry points.

## Pattern A — Publish as a public package

The simplest path. Publish to npm or PyPI under a normal package name, then add it to MCP Hero like any community stdio MCP:

```json
{
  "command": "npx",
  "args": ["-y", "@your-scope/your-server"]
}
```

For Python:

```json
{
  "command": "uvx",
  "args": ["your-server"]
}
```

`npx` and `uvx` pull the package from the public registry on the first **Start** and cache it for subsequent starts. No credentials, no extra setup.

This is the right pattern when:

- Public listing is acceptable. (The source can still live in a private GitHub repo; npm and PyPI only need the published artifact.)
- Version-bumping the package is an acceptable deployment mechanism. Every team that has the MCP installed picks up the new version on the next cold start.

## Pattern B — Publish as a private package and give the sandbox a token

When the package shouldn't be public, publish to a private registry (npm Pro, GitHub Packages, JFrog, AWS CodeArtifact, a private PyPI index) and give the sandbox a credential to fetch from it. The mechanism is the same as for upstream credentials: a Variable for the secret, optionally a File for the registry config. Two worked recipes follow, one per ecosystem.

### Private PyPI (uvx / pip): URL with embedded token

`uv` and `pip` both read their index URL from an environment variable and accept HTTP Basic credentials inside the URL. One Variable does it.

- **Variable** `PYPI_TOKEN` (Treat as password) — your registry token.
- **Variable** `UV_INDEX_URL` (plain) — `https://__token__:${PYPI_TOKEN}@pypi.your-company.com/simple/`.

In the JSON config:

```json
{
  "command": "uvx",
  "args": ["your-server"],
  "env": {
    "UV_INDEX_URL": "${UV_INDEX_URL}"
  }
}
```

(For pip-based installers, use `PIP_INDEX_URL` instead.)

### Private npm (GitHub Packages, etc.): `.npmrc` File + token Variable

npm reads registry auth from `.npmrc`, not from a single env var, so you need both a File and a Variable. The shape mirrors the GCP service-account recipe in [Stdio MCP authentication](stdio-authent.md).

- **Variable** `GITHUB_TOKEN` (Treat as password) — your PAT or registry token.
- **File**:
  - **Target path:** `${HOME}/.npmrc`
  - **Contents:**
    ```
    @your-scope:registry=https://npm.pkg.github.com
    //npm.pkg.github.com/:_authToken=${GITHUB_TOKEN}
    ```
- **JSON config:**

  ```json
  {
    "command": "npx",
    "args": ["-y", "@your-scope/your-server"],
    "env": {
      "GITHUB_TOKEN": "${GITHUB_TOKEN}"
    }
  }
  ```

Two `${...}` substitution layers cooperate here:

1. MCP Hero substitutes `${GITHUB_TOKEN}` in the `env` block at sandbox start, placing the secret in the process environment.
2. npm then substitutes `${GITHUB_TOKEN}` from its environment when reading `.npmrc`. (The token written into the File is a literal `${GITHUB_TOKEN}`; npm does that substitution, not MCP Hero.)

That's why the token also has to land in `env`: the `.npmrc` substitution is npm's, and npm only sees what's in the environment.

### Caveats

- **The 128 KiB File cap is on what you upload through MCP Hero**, not on what the package manager pulls down. The package itself can be arbitrarily large; only the registry config file lives in a File.
- **Disk size matters for big packages.** If the install footprint exceeds the default sandbox disk, raise it under **Sandbox → Resources**.
- **Token rotation** is a Variables edit. Update the value, save, and the next sandbox start picks it up. Existing live sessions keep the old value until they recycle.
- **Keep registry tokens marked Treat as password** so they're encrypted at rest and redacted from execution logs.

## When neither pattern fits

If your code is too heavy for an `npx` cold-start, has a non-trivial native build step, or you'd rather deploy by pushing to a server than by cutting a package release, the pragmatic alternative is to host the server yourself as a streamable-HTTP MCP and add it to MCP Hero as an HTTP MCP. You own the runtime; MCP Hero proxies. See [Upstream MCPs](upstream-mcps.md) → *Adding an HTTP MCP*.
