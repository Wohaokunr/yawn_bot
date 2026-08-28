# Redistributed runtime licenses

This directory contains notices for third-party components that YawnBot redistributes in its published runtime artifacts and that need clearer treatment than ordinary package metadata alone provides.

## nonebot-plugin-htmlkit 0.1.0rc5

YawnBot installs `nonebot-plugin-htmlkit==0.1.0rc5` in the runtime Python environment.

The upstream project documents two licensing layers:

- Python package code: MIT
- native `core` extension: LGPL-3.0-or-later

The published wheel contains `nonebot_plugin_htmlkit/core.abi3.so`, but its wheel metadata only carries the root MIT license. YawnBot therefore adds the missing LGPL/GPL license texts to the container image at build time from Debian's `/usr/share/common-licenses/` directory.

Corresponding upstream source for the exact version used by YawnBot is available as the PyPI source distribution:

`nonebot_plugin_htmlkit-0.1.0rc5.tar.gz`

Expected SHA256:

`5c9fc3ed1d1cbf95711006761d19e7a1dc0d0e8b7989c2e806a2bff3aeff7b17`

That source distribution records the bundled litehtml source at commit:

`2fcc6f567cca06d6682ffab3868632c4d9fcc673`

The HTMLKit Python MIT notice is copied in `nonebot-plugin-htmlkit-MIT.txt`. The matching litehtml BSD-3-Clause notice is copied in `litehtml-BSD-3-Clause.txt`.

## Other Python dependencies

Ordinary Python dependency license files remain inside their installed `*.dist-info/licenses` or equivalent package metadata directories in `/app/.venv` and are redistributed with the container image. This directory is intentionally limited to components where an extra top-level notice or missing upstream wheel notice is useful.

## Playwright browser payload

The Playwright-installed browser payload is redistributed under `/ms-playwright`. The installed Chromium/Chrome Headless Shell and FFmpeg payloads retain their upstream license/credits files inside that directory; YawnBot does not strip those files from the runtime image.
