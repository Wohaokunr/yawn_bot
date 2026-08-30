# Public release Compose

`compose.release.yaml` is the public-user deployment entrypoint for prebuilt GHCR YawnBot images.
It is intentionally separate from:

- repository-root `compose.yaml` — source build / development / clean-deploy CI;
- `deploy/production/compose.yaml` — maintainer production server deployment.

The YawnBot application image no longer embeds Chromium. Fanqie browser search is provided by the optional `fanqie-browser` Playwright sidecar so users who do not need it avoid the browser download entirely. The public sidecar is also a prebuilt GHCR artifact with a Playwright-version/runtime-hash tag.

Quick start from the repository root:

```bash
cp .env.example .env
# Edit SUPERUSERS and ONEBOT_V11_ACCESS_TOKEN first.
export YAWNBOT_IMAGE='ghcr.io/wohaokunr/yawn_bot:v0.1.0'
docker compose -f deploy/docker/compose.release.yaml pull
docker compose -f deploy/docker/compose.release.yaml up -d
curl --fail http://127.0.0.1:8080/healthz
```

To enable Fanqie browser search, add this to `.env`:

```dotenv
FANQIE_BROWSER_WS_ENDPOINT=ws://playwright:3000/
```

Then pull/start the optional Chromium headless sidecar:

```bash
docker compose -f deploy/docker/compose.release.yaml \
  --profile fanqie-browser pull
docker compose -f deploy/docker/compose.release.yaml \
  --profile fanqie-browser up -d
```

The sidecar is reachable only through the Compose network; do not publish its Playwright server port to the Internet. Source/development Compose may still build the sidecar locally; the public Release Compose never builds source.

Prefer the immutable `image@sha256:<digest>` recorded in each GitHub Release when possible.
The Compose file has no implicit application `latest` default on purpose.

Full guide: [`docs/public-docker-deployment.md`](../../docs/public-docker-deployment.md).
