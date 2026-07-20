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

5. **Open the WebUI** at `http://your-server:8484` (the container also gets a WebUI
   button in Unraid's Docker tab). Everything happens there — no console needed.

Alternatively, from any terminal on the server:

```bash
mkdir -p /mnt/user/appdata/applypilot-stack && cd /mnt/user/appdata/applypilot-stack
wget https://raw.githubusercontent.com/dylan34612/applypilot-unraid/main/docker-compose.yml
wget -O .env https://raw.githubusercontent.com/dylan34612/applypilot-unraid/main/.env.example
nano .env          # fill in your endpoint details
docker compose up -d
```

## The WebUI

The container's main process is a small FastAPI control panel (port 8484) that shells
out to the `applypilot` CLI, so behavior is identical to running commands by hand:

- **Setup** — edit `profile.json`, `searches.yaml`, and `resume.txt` in the browser
  (pre-seeded from ApplyPilot's example templates), plus optional `resume.pdf` upload.
  This replaces the interactive `applypilot init` wizard.
- **Pipeline** — buttons for Run (discover → enrich → score → tailor → cover → pdf),
  Doctor, Auto-apply dry-run, and Auto-apply live (with a confirmation prompt, since
  live mode submits real applications). One task runs at a time; Stop terminates it.
- **Console** — live streaming output of the running task.
- **Stats + results dashboard** — pipeline counters up top, and ApplyPilot's own HTML
  results dashboard (score charts, filterable job cards) regenerated on demand at
  `/dashboard`.

**No authentication** — keep it LAN-only. It can submit job applications under your
name; don't reverse-proxy it to the internet without adding auth.

The CLI still works too (`docker exec -it applypilot applypilot ...`), and setting the
stack's command to `idle` disables the WebUI entirely.

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

## Environment variables

Everything ApplyPilot reads, and where it's set:

| Variable | Purpose | Where |
|---|---|---|
| `LLM_URL` / `LLM_API_KEY` / `LLM_MODEL` | Your OpenAI-compatible endpoint for stages 1–5 | `.env` |
| `GEMINI_API_KEY` / `OPENAI_API_KEY` | Optional alternative providers (ignored while `LLM_URL` is set) | `.env` |
| `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` | Points the Claude Code CLI at the LiteLLM bridge | `.env` |
| `ANTHROPIC_API_KEY` | Real Anthropic API for auto-apply (instead of the bridge) | `.env` |
| `BRIDGE_UPSTREAM_BASE` / `BRIDGE_UPSTREAM_KEY` / `BRIDGE_UPSTREAM_MODEL` | Where the LiteLLM bridge forwards to | `.env` |
| `PROXY` | Scraping proxy (`host:port:user:pass`); passed through for parity, though the current ApplyPilot release never reads it | `.env` |
| `WEBUI_PORT` | Host port for the control panel (default 8484) | `.env` |
| `APPLYPILOT_DIR` | State directory (`/config`) | baked into image |
| `CHROME_PATH` | Container-safe Chromium wrapper | baked into image |

`CAPSOLVER_API_KEY` (CAPTCHA solving) is the one upstream variable deliberately not
wired up here; CAPTCHA-blocked applications are marked and skipped for you to finish
manually.

Escape hatch: ApplyPilot also loads `/config/.env` inside the container via dotenv
(without overriding compose-provided values), so if a future ApplyPilot version adds
new variables you can set them there with no compose changes.

## Notes

- The container serves the WebUI by default; set the service command to `idle` for a
  console-only container (`docker exec` still works either way).
- Chromium runs with `--no-sandbox` via a wrapper script — required inside Docker since
  ApplyPilot doesn't pass that flag itself. `shm_size: 2gb` is set for the same reason.
- The container runs as UID 99 / GID 100 (Unraid's `nobody:users`), which also satisfies
  Claude Code's refusal to run permission-bypassed as root.
- The LiteLLM bridge is only reachable inside the compose network; no ports are published.
- If `docker compose pull` returns 401/denied: the GHCR package may still be private —
  on GitHub go to the package's settings and set visibility to Public.
- ApplyPilot is AGPL-3.0. Auto-applying may violate the terms of service of some job
  boards — use judgment about where you point it.
