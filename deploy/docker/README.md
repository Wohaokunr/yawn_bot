# Public release Compose

`compose.release.yaml` is the public-user deployment entrypoint for prebuilt GHCR images.
It is intentionally separate from:

- repository-root `compose.yaml` — source build / development / clean-deploy CI;
- `deploy/production/compose.yaml` — maintainer production server deployment.

Quick start from the repository root:

```bash
cp .env.example .env
# Edit SUPERUSERS and ONEBOT_V11_ACCESS_TOKEN first.
export YAWNBOT_IMAGE='ghcr.io/wohaokunr/yawn_bot:v0.1.0'
docker compose -f deploy/docker/compose.release.yaml pull
docker compose -f deploy/docker/compose.release.yaml up -d
curl --fail http://127.0.0.1:8080/healthz
```

Prefer the immutable `image@sha256:<digest>` recorded in each GitHub Release when possible.
The Compose file has no implicit `latest` default on purpose.

Full guide: [`docs/public-docker-deployment.md`](../../docs/public-docker-deployment.md).
