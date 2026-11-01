# syntax=docker/dockerfile:1.7

FROM node:22-bookworm-slim AS webui-builder
WORKDIR /build/webui
COPY webui/package.json webui/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm \
    npm ci
COPY webui/ ./
RUN npm run build

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

# Agent 代码沙箱的独立 venv：只给群成员的代码用，和 Bot 依赖完全分开。
# requirements.txt 带哈希锁定；删掉各包自带的测试目录后预编译字节码——
# 沙箱根文件系统只读，没有 .pyc 时每次运行都要多花约 2 秒重新编译。
FROM python:3.12-slim-trixie AS sandbox-builder
COPY --from=ghcr.io/astral-sh/uv:0.12.6 /uv /uvx /bin/
ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0
COPY sandbox/requirements.txt /tmp/sandbox-requirements.txt
RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv --seed /opt/yawnbot-sandbox/venv --python /usr/local/bin/python3.12 \
    && uv pip install --python /opt/yawnbot-sandbox/venv/bin/python \
        --require-hashes --no-deps -r /tmp/sandbox-requirements.txt \
    && find /opt/yawnbot-sandbox/venv/lib -type d -name tests -prune -exec rm -rf {} + \
    && /opt/yawnbot-sandbox/venv/bin/python -m compileall -q -j 0 /opt/yawnbot-sandbox/venv/lib

FROM python:3.12-slim-trixie AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:${PATH}" \
    HOST=0.0.0.0 \
    PORT=8080 \
    LOCALSTORE_USE_CWD=true \
    YAWNBOT_PINNED_ENV_KEYS=HOST,PORT,LOCALSTORE_USE_CWD \
    YAWNBOT_SUPERVISED=1
WORKDIR /app

# HTMLKit uses Fontconfig at runtime. The slim base image has no CJK font, so
# visual cards such as /help would render Chinese text as missing glyphs.
# WenQuanYi Micro Hei keeps the added image layer small while covering CJK.
# file/jq/tree/zip/unzip/bc 给代码沙箱终端（run_shell）用；bash、grep、sed、
# find、coreutils 基础镜像自带。curl/wget/git/ca-certificates 让联网的虚拟电脑能
# 下载文件；iptables 给沙箱 runner 挡内网、按工作区开关外网（见 netpolicy.py）；
# xz-utils 给能力包处理 .xz。
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        fontconfig \
        fonts-wqy-microhei \
        file \
        jq \
        tree \
        unzip \
        zip \
        bc \
        ca-certificates \
        curl \
        wget \
        git \
        iptables \
        xz-utils \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/*

# Keep ownership setup in a stable layer. Mutable source copies below use
# COPY --chown so normal application changes do not force a recursive chown
# over the large Python virtualenv.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin yawnbot \
    && mkdir -p /app/data /opt/yawnbot \
    && chown yawnbot:yawnbot /app /app/data /opt/yawnbot

COPY --chown=10001:10001 --from=python-builder /app/.venv /app/.venv
COPY --chown=10001:10001 --from=python-builder /app/third_party_sources /app/third_party_sources

COPY --chown=10001:10001 pyproject.toml LICENSE THIRD_PARTY_NOTICES.md ./
COPY --chown=10001:10001 third_party_licenses ./third_party_licenses

# System license copies are tiny and intentionally happen before mutable app
# source layers so source-only releases do not invalidate this setup step.
RUN test -f /usr/share/common-licenses/GPL-3 \
    && test -f /usr/share/common-licenses/LGPL-3 \
    && cp /usr/share/common-licenses/GPL-3 /app/third_party_licenses/GPL-3.0.txt \
    && cp /usr/share/common-licenses/LGPL-3 /app/third_party_licenses/LGPL-3.0.txt

# 代码沙箱：venv、助手库 yawnkit 与 runner 都归 root、只读；runner 以 sandbox
# 服务单独运行（见 compose 的 sandbox 服务），Bot 容器本身不会执行这些代码。
COPY --from=sandbox-builder /opt/yawnbot-sandbox/venv /opt/yawnbot-sandbox/venv
COPY sandbox/yawnkit /opt/yawnbot-sandbox/lib/yawnkit
COPY sandbox/yawn_sandbox /opt/yawnbot-sandbox/runner/yawn_sandbox
# 预生成 matplotlib 字体缓存（必须在装好中文字体的运行阶段生成）。
RUN MPLCONFIGDIR=/opt/yawnbot-sandbox/mplcache /opt/yawnbot-sandbox/venv/bin/python -c \
        "import matplotlib; matplotlib.use('Agg'); import matplotlib.font_manager as fm; fm.findfont('WenQuanYi Micro Hei')" \
    && python -m compileall -q /opt/yawnbot-sandbox/lib /opt/yawnbot-sandbox/runner \
    && chmod -R a+rX,go-w /opt/yawnbot-sandbox

COPY --chown=10001:10001 src ./src
COPY --chown=10001:10001 data/nonebot_plugin_orm/migrations /opt/yawnbot/migrations
COPY --chown=10001:10001 --from=webui-builder /build/webui/dist ./webui/dist
COPY --chmod=755 deploy/docker-entrypoint.sh /usr/local/bin/yawnbot-entrypoint
COPY --chmod=644 deploy/docker-load-env.py /usr/local/bin/yawnbot-load-env

USER yawnbot
EXPOSE 8080
VOLUME ["/app/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).read()" || exit 1

ENTRYPOINT ["yawnbot-entrypoint"]
CMD ["nb", "run"]
