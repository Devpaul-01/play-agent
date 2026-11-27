# Sandbox image for play-agent (PLAY_AGENT_SANDBOX_IMAGE).
#
# The agent mounts a scratch copy of the app under test at /app and runs
# `npx playwright test` inside this container, so the image only needs Node
# and a Playwright/browser install whose version matches the app's
# @playwright/test dependency. Build with the app's resolved version:
#
#   docker build -f docker/sandbox.Dockerfile \
#     --build-arg PLAYWRIGHT_VERSION=<version from the app's package-lock.json> \
#     -t play-agent-sandbox:latest .
ARG PLAYWRIGHT_VERSION=1.49.1
FROM mcr.microsoft.com/playwright:v${PLAYWRIGHT_VERSION}-noble

WORKDIR /app

# The sandbox starts this container detached with a TTY and then execs
# commands into it, so it must stay alive until it is torn down.
CMD ["sleep", "infinity"]
