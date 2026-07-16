# ApplyPilot on Unraid

Pull-and-run Docker deployment for [ApplyPilot](https://github.com/Pickle-Pixel/ApplyPilot)
using a custom OpenAI-compatible LLM endpoint. ApplyPilot ships no Docker image, so this
repo builds one via GitHub Actions: Python 3.11 + Node 20 + Chromium + Claude Code CLI +
ApplyPilot, published to `ghcr.io/dylan34612/applypilot-unraid:latest`.

## How the LLM backends work

ApplyPilot uses AI in two distinct places:

| Stages | What | LLM backend |
|---|---|---|
| 1–5: discover, enrich, score, tailor, cover letters | Plain chat-completion calls | **Any OpenAI-compatible endpoint** — natively supported via `LLM_URL` / `LLM_API_KEY` / `LLM_MODEL` |
| 6: auto-apply (browser form filling) | Spawns the `claude` CLI with a Playwright MCP server | Anthropic Messages API — bridged to your endpoint via a LiteLLM sidecar (included) |

**Is Claude Code required?** Only for stage 6, and only the *CLI program* — not an
Anthropic subscription. ApplyPilot shells out to `claude -p --output-format stream-json`
and parses that exact output, so swapping in a different agent CLI would mean forking
`apply/launcher.py`. Instead, this setup keeps the CLI but points it at your own endpoint
through a LiteLLM sidecar that translates Anthropic ⇄ OpenAI API formats. The sidecar runs
in single-model mode: every request Claude Code makes is forwarded to
`BRIDGE_UPSTREAM_MODEL` no matter what model name is asked for. If you never run
`applypilot apply`, Claude Code is never invoked and stages 1–5 run entirely off your
endpoint.

**Honest caveat:** form navigation is long-horizon agentic tool-calling. Small or local
models routinely lose the plot mid-form. If your endpoint serves a strong tool-calling
model it can work; otherwise use stages 1–5 only, or a real Anthropic key just for the
apply stage. Always test with `applypilot apply --dry-run` (fills forms, never submits)
before going live.

## Setup with the Docker Compose Manager plugin

1. **Install the plugin** (Unraid 6.x: Apps → *Docker Compose Manager*; Unraid 7 has
   compose support built in).

2. **Create a stack** named `applypilot`, and paste in
   [docker-compose.yml](docker-compose.yml).

3. **Set the env** — in the stack's env file editor, paste the contents of
   [.env.example](.env.example) and fill in your endpoint URL, API key, and model name
   (the `BRIDGE_*` values are usually the same endpoint as `LLM_*`).

4. **Compose Up.** The image is pulled from GHCR — nothing is built on the server.

Alternatively, from any terminal on the server:

```bash
mkdir -p /mnt/user/appdata/applypilot-stack && cd /mnt/user/appdata/applypilot-stack
wget https://raw.githubusercontent.com/dylan34612/applypilot-unraid/main/docker-compose.yml
wget -O .env https://raw.githubusercontent.com/dylan34612/applypilot-unraid/main/.env.example
nano .env          # fill in your endpoint details
docker compose up -d
```

## First-time init and usage

The setup wizard is interactive, so run it in a console (Unraid UI → applypilot
container → Console, or any terminal):

```bash
docker exec -it applypilot applypilot init      # profile, resume, searches
docker exec -it applypilot applypilot doctor    # verify everything is wired up
```

`init` asks for a Gemini key — skip it; your endpoint is already configured via the
`LLM_URL` environment variable, which takes precedence. State lives in
`/mnt/user/appdata/applypilot/config` on the host.

```bash
# Discover, enrich, score, tailor, write cover letters
docker exec -it applypilot applypilot run all

# Auto-apply (headless Chrome), dry-run first
docker exec -it applypilot applypilot apply --headless --dry-run
docker exec -it applypilot applypilot apply --headless

# Pipeline status
docker exec -it applypilot applypilot status
```

**Scheduling (optional):** Unraid's *User Scripts* plugin with a cron schedule:

```bash
#!/bin/bash
docker exec applypilot applypilot run all
```

## Updating

The image rebuilds weekly (and on every push) via GitHub Actions, picking up new
ApplyPilot and Claude Code releases. On the server: re-run Compose Up after
`docker compose pull`, or use the plugin's update button.

To build locally instead of pulling:

```bash
git clone https://github.com/dylan34612/applypilot-unraid
cd applypilot-unraid
docker build -t ghcr.io/dylan34612/applypilot-unraid:latest .
```

## Notes

- The container idles by default so you can `docker exec` commands into it.
- Chromium runs with `--no-sandbox` via a wrapper script — required inside Docker since
  ApplyPilot doesn't pass that flag itself. `shm_size: 2gb` is set for the same reason.
- The container runs as UID 99 / GID 100 (Unraid's `nobody:users`), which also satisfies
  Claude Code's refusal to run permission-bypassed as root.
- The LiteLLM bridge is only reachable inside the compose network; no ports are published.
- If `docker compose pull` returns 401/denied: the GHCR package may still be private —
  on GitHub go to the package's settings and set visibility to Public.
- ApplyPilot is AGPL-3.0. Auto-applying may violate the terms of service of some job
  boards — use judgment about where you point it.
