# `platform-lab` Dev Container

Sandboxed environment for running Claude Code unattended (`--dangerously-skip-permissions`) against this repo. Based on Anthropic's reference dev container (`anthropics/claude-code/.devcontainer`), adapted for a Python/FastAPI project with a tighter network allowlist.

## Why this exists

Letting an agentic coding tool run without permission prompts is only safe inside a boundary that limits what it can reach. This container provides that boundary via:
- Non-root user (`node`) — required by Claude Code to accept `--dangerously-skip-permissions` at all
- Default-deny network firewall — only specific domains are reachable
- Bind-mounted repo only — nothing else on the host is visible
- Container-scoped Claude Code credential — isolated from the host's real `~/.claude`

## Files

| File | Purpose |
|---|---|
| `devcontainer.json` | VS Code / CLI configuration — mounts, env vars, capabilities, lifecycle hooks |
| `Dockerfile` | Image definition — Node (for Claude Code) + Python/`uv` on top |
| `init-firewall.sh` | Default-deny `iptables`/`ipset` firewall, run via `postStartCommand` |

## Key decisions

**`.claude` credential — named volume, not a host bind:**
```jsonc
"mounts": [
  "source=claude-code-config-${devcontainerId},target=/home/node/.claude,type=volume"
]
```
Keeps the container's Claude Code login independent from the host's real credential. If this container is ever compromised, only a container-scoped session leaks — not the credential used everywhere else. Persists across container restarts and rebuilds; you only re-authenticate if the volume itself is deleted.

**GitHub access — read-only by design, enforced by absence of credentials, not by the firewall:**
A firewall can't distinguish `git pull` from `git push` — both are the same network connection. Read-only is achieved by *not* mounting any write-capable credential (no `~/.ssh`, no full-access PAT) inside the container. `github.com`, `api.github.com`, `raw.githubusercontent.com`, and `objects.githubusercontent.com` are allowed in the firewall so clone/fetch/pull of public repos works over anonymous HTTPS. Any `git push` attempt fails on missing auth, not on network — verified by testing an actual push and confirming it fails with an auth error, not a connection timeout.

**API key passthrough — via `remoteEnv`, not `containerEnv`:**
```jsonc
"remoteEnv": {
  "ANTHROPIC_API_KEY": "${localEnv:ANTHROPIC_API_KEY}",
  "GROQ_API_KEY": "${localEnv:GROQ_API_KEY}"
}
```
`containerEnv` values are visible via `docker inspect`; `remoteEnv` only injects into the actual session, so it doesn't sit in container metadata. Both keys are forwarded from the host shell's environment (`localEnv`) — separate from the app's own `.env` file, which is only read when the app runs via `load_dotenv()` (e.g. inside `docker compose`, where `.env` is picked up directly by Compose).

## Firewall allowlist (current)

```
registry.npmjs.org          — Claude Code install (npm)
api.anthropic.com            — Claude Code's actual API endpoint; also the app's optional LLM_PROVIDER=anthropic
api.groq.com                 — the app's default LLM_PROVIDER (metrics analysis + RAG)
sentry.io                    — Claude Code's own error reporting
marketplace.visualstudio.com — VS Code extension installs (GUI use only)
vscode.blob.core.windows.net — VS Code Server download (GUI use only)
update.code.visualstudio.com — VS Code Server version check (GUI use only)
pypi.org                     — Python package metadata
files.pythonhosted.org       — Python package downloads
github.com                   — clone/fetch, public repos
api.github.com               — GitHub API (gh CLI, WebFetch on repo metadata)
raw.githubusercontent.com    — raw file content
objects.githubusercontent.com — Git LFS / release assets
```

**Deliberately excluded:** `statsig.anthropic.com`/`statsig.com` — Anthropic's internal telemetry, removed after it caused a DNS-resolution build failure (non-essential to Claude Code functioning).

## Known gotchas (hit and fixed during setup)

**1. `statsig.anthropic.com` DNS failure kills the whole firewall script.**
The domain loop treats every entry as fatal-if-unresolvable (`set -e` + explicit `exit 1` on failure). One flaky/blocked domain took down the entire firewall setup. Fix: removed the domain rather than making failures non-fatal — the strict-fail behavior is otherwise correct and worth keeping.

**2. `ipset add` errors on duplicate IPs.**
Two allowed domains can resolve to the same IP (common with CDN-hosted services). Default `ipset add` treats a duplicate as an error, which — again under `set -e` — kills the whole script. Fix: add `-exist` to every `ipset add allowed-domains "$ip"` call so duplicates are a no-op instead of a fatal error.

**3. Docker build cache serves a stale `init-firewall.sh` after editing it.**
`COPY init-firewall.sh /usr/local/bin/` happens at image build time. Editing the source file and just recreating the *container* isn't enough if Docker reuses a cached image layer — the old script stays baked in. Fix: rebuild with both flags together:
```bash
devcontainer up --workspace-folder ~/src/platform-lab --build-no-cache --remove-existing-container
```
Verify the fix actually landed before trusting it:
```bash
devcontainer exec --workspace-folder ~/src/platform-lab cat /usr/local/bin/init-firewall.sh | grep -A5 "for domain in"
```

**4. VS Code and the standalone CLI generate different container labels for the same folder.**
The CLI (`devcontainer up` from a WSL2 terminal) labels the container with a native Linux path (`devcontainer.local_folder=/home/hagop/src/platform-lab`). VS Code's Dev Containers extension — even correctly connected via WSL Remote — labels it with a Windows UNC-style path (`\\wsl.localhost\...`) instead. This is a known inconsistency between the two tools, not user error, and results in **two separate containers** for the same project if you use "Reopen in Container" after already building via the CLI.

Fix: don't rely on automatic label matching across tools. Build via the CLI as the source of truth, then in VS Code use:
```
Ctrl+Shift+P → Dev Containers: Attach to Running Container...
```
— **not** "Reopen in Container" — to connect to the CLI-built container directly, bypassing label comparison entirely.

**5. `shutdownAction` defaults to stopping the container when VS Code closes — but this is unreliably enforced on WSL2.**
For unattended work you don't want VS Code closing to kill anything. Set explicitly:
```jsonc
"shutdownAction": "none"
```
Note this only controls the *container* — it does not protect against the *host machine sleeping*, which suspends the whole WSL2 VM regardless of any devcontainer setting. Disable Windows sleep separately for any long unattended run.

## Day-to-day commands

```bash
# Build / start
devcontainer up --workspace-folder ~/src/platform-lab

# Run a one-off command inside it
devcontainer exec --workspace-folder ~/src/platform-lab <command>

# Authenticate Claude Code (first time, or after volume deletion)
devcontainer exec --workspace-folder ~/src/platform-lab claude

# Verify the firewall is actually enforcing what's documented above
devcontainer exec --workspace-folder ~/src/platform-lab curl -m 5 https://pypi.org      # expect: success
devcontainer exec --workspace-folder ~/src/platform-lab curl -m 5 https://example.com   # expect: fail

# Run unattended (wrap in tmux if you want it to survive closing the terminal)
tmux new -s claude-session
devcontainer exec --workspace-folder ~/src/platform-lab claude --dangerously-skip-permissions
# Ctrl+B, D to detach; `tmux attach -t claude-session` to resume

# Full clean rebuild (container + image, no cache)
docker ps -a --filter "label=devcontainer.config_file=/home/hagop/src/platform-lab/.devcontainer/devcontainer.json" -q | xargs -r docker rm -f
devcontainer up --workspace-folder ~/src/platform-lab --build-no-cache
```

## Avoiding lost sessions

Each `devcontainer exec ... claude` invocation starts a process — if it exits (intentionally or not), the conversation isn't gone, but you do need one of the two options below to get back to it without losing context.

**Option 1 — Resume the most recent session:**

```bash
devcontainer exec --workspace-folder $(pwd) env TERM=xterm-256color claude --continue
```

Picks up the last session in this project with full context intact. To choose from multiple past sessions instead of just the most recent:

```bash
devcontainer exec --workspace-folder $(pwd) env TERM=xterm-256color claude
# then inside: /resume
```

**Option 2 — Keep it running via tmux, don't exit at all:**

```bash
devcontainer exec --workspace-folder $(pwd) tmux new -s claude-session
claude --dangerously-skip-permissions
# Ctrl+B, then D to detach — claude keeps running
```

Reattach later, from the same or a different terminal:

```bash
devcontainer exec --workspace-folder $(pwd) tmux attach -t claude-session
```

This is the right choice for genuinely unattended work — the process never stops, so there's no "resume" needed. `--continue`/`--resume` is the fallback for when a session did end (crash, accidental exit, closed terminal without tmux); tmux is what prevents that from happening in the first place.

## Remaining hardening not yet done (candidates for revisit)

- Host-network `/24` firewall rule is broader than strictly necessary (grants the container access to the whole local subnet, not just this machine) — acceptable for this low-stakes public repo, worth narrowing before reuse on a higher-stakes project.
- No inner `/sandbox` (Bash sandbox) layered inside the container yet — optional additional per-command restriction on top of the container boundary.
