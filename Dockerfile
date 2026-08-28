# syntax=docker/dockerfile:1.7

FROM node:22-bookworm-slim AS webui-builder
WORKDIR /build/webui
COPY webui/package.json webui/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm \
    npm ci
COPY webui/ ./
RUN npm run build

FROM python:3.12-slim-trixie AS browser-runtime
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
COPY deploy/docker/playwright-version.txt /tmp/playwright-version.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    playwright_version="$(cat /tmp/playwright-version.txt)" \
    && python -m pip install "playwright==${playwright_version}" \
    && playwright install --with-deps chromium \
    && python -m pip uninstall -y playwright pyee greenlet \
    && chmod -R a+rX /ms-playwright \
    && rm -rf /var/lib/apt/lists/* /tmp/playwright-version.txt

FROM python:3.12-slim-trixie AS python-builder
COPY --from=ghcr.io/astral-sh/uv:0.12.6 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

# Keep the exact corresponding source archive next to the redistributed HTMLKit
# native extension. The digest is the one published by PyPI for 0.1.0rc5.
RUN mkdir -p /app/third_party_sources \
    && python -c "import urllib.request; urllib.request.urlretrieve('https://files.pythonhosted.org/packages/29/79/7f0b60a19132e335cb2d6ec2902e330afc97e82462dc152ae033864acf25/nonebot_plugin_htmlkit-0.1.0rc5.tar.gz', '/app/third_party_sources/nonebot_plugin_htmlkit-0.1.0rc5.tar.gz')" \
    && echo "5c9fc3ed1d1cbf95711006761d19e7a1dc0d0e8b7989c2e806a2bff3aeff7b17  /app/third_party_sources/nonebot_plugin_htmlkit-0.1.0rc5.tar.gz" | sha256sum -c -

FROM browser-runtime AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:${PATH}" \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    HOST=0.0.0.0 \
    PORT=8080 \
    LOCALSTORE_USE_CWD=true
WORKDIR /app

COPY --from=python-builder /app/.venv /app/.venv
COPY --from=python-builder /app/third_party_sources /app/third_party_sources

COPY pyproject.toml LICENSE THIRD_PARTY_NOTICES.md ./
COPY third_party_licenses ./third_party_licenses
COPY src ./src
COPY data/nonebot_plugin_orm/migrations /opt/yawnbot/migrations
COPY --from=webui-builder /build/webui/dist ./webui/dist
COPY deploy/docker-entrypoint.sh /usr/local/bin/yawnbot-entrypoint

RUN test -f /usr/share/common-licenses/GPL-3 \
    && test -f /usr/share/common-licenses/LGPL-3 \
    && cp /usr/share/common-licenses/GPL-3 /app/third_party_licenses/GPL-3.0.txt \
    && cp /usr/share/common-licenses/LGPL-3 /app/third_party_licenses/LGPL-3.0.txt \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin yawnbot \
    && mkdir -p /app/data \
    && chown -R yawnbot:yawnbot /app /opt/yawnbot \
    && chmod +x /usr/local/bin/yawnbot-entrypoint

USER yawnbot
EXPOSE 8080
VOLUME ["/app/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).read()" || exit 1

ENTRYPOINT ["yawnbot-entrypoint"]
CMD ["nb", "run"]
