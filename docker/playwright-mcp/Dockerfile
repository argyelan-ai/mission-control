# playwright-mcp — the MCP server Claude agents drive the shared agent browser
# with. It never launches a browser of its own: it attaches to cdp-browser over
# CDP (--cdp-endpoint, see docker-compose.yml), so no browser is downloaded and
# the image is ~0.37 GB instead of the 1.7 GB of mcr.microsoft.com/playwright/mcp.
# The npm version is pinned exactly; MCR had no image for it (newest v0.0.82).
FROM node:22-bookworm-slim@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392

ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 NODE_ENV=production
RUN npm install -g @playwright/mcp@0.0.83 && npm cache clean --force

# Named screenshots (`browser_take_screenshot(filename="<task_id>/x.png")`)
# resolve against the working directory and must stay inside the allowed
# roots: only with WORKDIR = the --output-dir mount do they land in the
# shared folder the backend reads (lab: without it 0.0.83 refuses "outside
# allowed roots", the old 0.0.70 image wrote into /home/node and failed).
RUN mkdir -p /output && chown node:node /output
WORKDIR /output
USER node
ENTRYPOINT ["playwright-mcp"]
