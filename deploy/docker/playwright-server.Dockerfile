# syntax=docker/dockerfile:1.7

FROM node:22-bookworm-slim

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PLAYWRIGHT_SERVER_PORT=3000

COPY deploy/docker/playwright-version.txt /tmp/playwright-version.txt
RUN --mount=type=cache,target=/root/.npm \
    playwright_version="$(cat /tmp/playwright-version.txt)" \
    && npm install --global "playwright@${playwright_version}" \
    && playwright install --with-deps --only-shell chromium \
    && chmod -R a+rX /ms-playwright \
    && npm cache clean --force \
    && rm -rf /var/lib/apt/lists/* /tmp/playwright-version.txt

USER node
WORKDIR /home/node
EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD node -e "const net=require('net');const s=net.connect(3000,'127.0.0.1',()=>{s.end();process.exit(0)});s.setTimeout(3000,()=>process.exit(1));s.on('error',()=>process.exit(1));"

CMD ["playwright", "run-server", "--port", "3000", "--host", "0.0.0.0"]
