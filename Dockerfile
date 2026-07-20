FROM python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive

# Chromium (auto-apply browser), Node.js 20 (Playwright MCP + Claude Code CLI),
# lsof/procps (ApplyPilot's zombie-Chrome cleanup uses them)
RUN apt-get update && apt-get install -y --no-install-recommends \
        chromium lsof procps curl ca-certificates gnupg git \
        fonts-liberation fonts-noto-color-emoji \
        # Virtual display + noVNC so headed Chrome is viewable in a browser
        # (watch the agent live, solve CAPTCHAs by hand)
        xvfb x11vnc fluxbox novnc websockify \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code @playwright/mcp

# Build ApplyPilot from source (matches how an in-repo Dockerfile would work,
# and validates against upstream HEAD rather than the last PyPI release).
# python-jobspy pins an exact numpy in its metadata that breaks pip's resolver;
# --no-deps + manual deps is the install path the ApplyPilot README prescribes.
ARG APPLYPILOT_REPO=https://github.com/Pickle-Pixel/ApplyPilot
ARG APPLYPILOT_REF=main
RUN git clone --depth 1 --branch "${APPLYPILOT_REF}" "${APPLYPILOT_REPO}" /tmp/applypilot-src \
    && pip install --no-cache-dir /tmp/applypilot-src \
    && pip install --no-cache-dir --no-deps python-jobspy \
    && pip install --no-cache-dir pydantic tls-client requests markdownify regex \
    # WebUI editor seed templates come from the same source tree
    && mkdir -p /opt/webui \
    && cp /tmp/applypilot-src/profile.example.json /opt/webui/ \
    && cp /tmp/applypilot-src/src/applypilot/config/searches.example.yaml /opt/webui/ \
    && rm -rf /tmp/applypilot-src

# WebUI (control panel served on port 8484)
RUN pip install --no-cache-dir fastapi uvicorn python-multipart
COPY webui /opt/webui

# ApplyPilot launches Chrome without --no-sandbox, which fails inside a
# container; CHROME_PATH points at this wrapper instead of the raw binary
RUN printf '#!/bin/sh\nexec /usr/bin/chromium --no-sandbox --disable-dev-shm-usage "$@"\n' \
        > /usr/local/bin/chromium-container \
    && chmod +x /usr/local/bin/chromium-container

# Unraid convention: nobody:users = 99:100. Non-root is also required because
# Claude Code refuses --permission-mode bypassPermissions as root.
RUN useradd -u 99 -g 100 -m -s /bin/bash pilot \
    && mkdir -p /config && chown 99:100 /config

ENV APPLYPILOT_DIR=/config \
    CHROME_PATH=/usr/local/bin/chromium-container \
    HOME=/home/pilot

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENV DISPLAY=:99 \
    VNC_RESOLUTION=1280x900x24 \
    NOVNC_PORT=8485

USER pilot
WORKDIR /home/pilot
VOLUME /config
EXPOSE 8484 8485

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["webui"]
