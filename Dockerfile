FROM python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive

# Chromium (auto-apply browser), Node.js 20 (Playwright MCP + Claude Code CLI),
# lsof/procps (ApplyPilot's zombie-Chrome cleanup uses them)
RUN apt-get update && apt-get install -y --no-install-recommends \
        chromium lsof procps curl ca-certificates gnupg git \
        fonts-liberation fonts-noto-color-emoji \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code @playwright/mcp

# python-jobspy pins an exact numpy in its metadata that breaks pip's resolver;
# --no-deps + manual deps is the install path the ApplyPilot README prescribes
RUN pip install --no-cache-dir applypilot \
    && pip install --no-cache-dir --no-deps python-jobspy \
    && pip install --no-cache-dir pydantic tls-client requests markdownify regex

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

USER pilot
WORKDIR /home/pilot
VOLUME /config

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["idle"]
