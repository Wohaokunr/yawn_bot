# Redistributed runtime licenses

This directory contains notices for third-party components that YawnBot redistributes in its published runtime artifacts and that need clearer treatment than ordinary package metadata alone provides.

## nonebot-plugin-htmlkit 0.1.0rc5

YawnBot installs `nonebot-plugin-htmlkit==0.1.0rc5` in the runtime Python environment and redistributes the upstream wheel unmodified.

The upstream project documents two licensing layers:

- Python package code: MIT
- native `core` extension: LGPL-3.0-or-later

The published wheel contains `nonebot_plugin_htmlkit/core.abi3.so`, but its wheel metadata only carries the root MIT license. YawnBot therefore adds the missing LGPL/GPL license texts to the container image at build time from Debian's `/usr/share/common-licenses/` directory.

Corresponding upstream source for the exact version used by YawnBot is the PyPI source distribution:

`nonebot_plugin_htmlkit-0.1.0rc5.tar.gz`

Expected SHA256:

`5c9fc3ed1d1cbf95711006761d19e7a1dc0d0e8b7989c2e806a2bff3aeff7b17`

Published runtime images contain that verified archive under `/app/third_party_sources/`. GitHub Releases also attach the same archive and include it in `SHA256SUMS.txt`.

The exact upstream release commit is:

`ff270a627280dfc4e88b38ed465dee5bdc7d984f`

Its repository tree records the bundled litehtml source at commit:

`2fcc6f567cca06d6682ffab3868632c4d9fcc673`

The HTMLKit Python MIT notice is copied in `nonebot-plugin-htmlkit-MIT.txt`. The matching litehtml BSD-3-Clause notice is copied in `litehtml-BSD-3-Clause.txt`.

`nonebot-plugin-htmlkit-native-provenance.md` records the direct native package inputs declared by upstream `xmake.lua` and documents the limitation that the upstream wheel does not expose exact-version metadata for every statically incorporated native library. Any HTMLKit version change requires a fresh review.

## Other Python dependencies

Ordinary Python dependencies generally retain their license files inside installed `*.dist-info/licenses` or equivalent package metadata directories in `/app/.venv`. Exceptions where upstream wheels do not contain a standalone license file are documented explicitly in `THIRD_PARTY_NOTICES.md` and pinned by the fail-closed license audit.

The current manual-review exceptions are documented in `THIRD_PARTY_NOTICES.md`:

- `text-unidecode`: YawnBot uses the upstream Artistic License option rather than the alternative GPL option.
- `cookiecutter`: scanner metadata may report `UNKNOWN`; upstream is BSD-licensed.
- `nonestorage`: scanner metadata may report `UNKNOWN`; upstream is MIT-licensed.
- `certifi`: MPL-2.0 and retains a standalone license file in the installed wheel.
- `tqdm==4.70.0`: MPL-2.0 AND MIT; its wheel has no standalone `LICENSE`/`COPYING` file, so the reviewed version/license expression is enforced by `tools/license_audit.py` and the exception is recorded in `THIRD_PARTY_NOTICES.md`.

This directory is intentionally limited to components where an extra top-level notice, missing upstream wheel notice, or provenance record is useful.

## WebUI dependencies

The production WebUI dependency tree is audited separately. The current production tree contains MIT, ISC and OFL-1.1 packages only. The redistributed ZCOOL KuaiLe font license is copied in `ZCOOL-KuaiLe-OFL-1.1.txt`.

## Playwright browser payload

The Playwright-installed browser payload is redistributed under `/ms-playwright`. The installed Chromium/Chrome Headless Shell and FFmpeg payloads retain their upstream license/credits files inside that directory; YawnBot does not strip those files from the runtime image.
