# nonebot-plugin-htmlkit native provenance

YawnBot redistributes the upstream `nonebot-plugin-htmlkit==0.1.0rc5` wheel unmodified inside its runtime Python environment.

## Exact upstream release

- Package: `nonebot-plugin-htmlkit==0.1.0rc5`
- Release commit: `ff270a627280dfc4e88b38ed465dee5bdc7d984f`
- Source distribution: `nonebot_plugin_htmlkit-0.1.0rc5.tar.gz`
- Source distribution SHA256: `5c9fc3ed1d1cbf95711006761d19e7a1dc0d0e8b7989c2e806a2bff3aeff7b17`
- Upstream source repository: `https://github.com/nonebot/plugin-htmlkit`
- litehtml submodule commit at that release: `2fcc6f567cca06d6682ffab3868632c4d9fcc673`

The exact source distribution is copied into published YawnBot runtime images at `/app/third_party_sources/nonebot_plugin_htmlkit-0.1.0rc5.tar.gz`. YawnBot GitHub Releases also attach the same archive and cover it with `SHA256SUMS.txt`.

## Native build inputs

At the exact release commit above, upstream `xmake.lua` declares the native `core` target as LGPL-3.0-or-later and builds it as a shared library. Its direct package inputs are:

- litehtml
- pango
- libjpeg-turbo
- libwebp
- giflib
- aklomp-base64
- fmt
- libavif (with AOM enabled)
- cairo
- Python headers/runtime

The upstream build configuration requests non-system, non-shared package builds for dependencies other than Python/build tools. Therefore consumers should treat the upstream native wheel as containing third-party native code in addition to HTMLKit's own `core` implementation.

YawnBot does not modify or relink the upstream HTMLKit wheel. The Python package metadata does not expose an exact-version SBOM for every statically incorporated native build input, so this document deliberately does not claim version-level provenance for those nested native libraries. Upgrading `nonebot-plugin-htmlkit` requires a fresh license/provenance review.

## License handling

- HTMLKit Python code: MIT; copy in `nonebot-plugin-htmlkit-MIT.txt`.
- HTMLKit native `core`: LGPL-3.0-or-later; the runtime image includes LGPL-3.0 and GPL-3.0 license texts under `/app/third_party_licenses/`.
- litehtml: BSD-3-Clause; copy in `litehtml-BSD-3-Clause.txt`.
- Other native components retain their upstream licenses. Their source projects and notices remain the authoritative source for component-specific terms.

This file is an engineering provenance record, not a legal opinion. It exists so that a dependency upgrade cannot silently hide the native redistribution boundary.
