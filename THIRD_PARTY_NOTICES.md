# Third-Party Notices

YawnBot includes or redistributes third-party components under their own licenses.
This file records components that need an explicit top-level redistribution notice.
Ordinary package dependencies retain their own package metadata and license files in
the installed runtime environment.

## ZCOOL KuaiLe

The WebUI uses `@fontsource/zcool-kuaile`, which redistributes the ZCOOL KuaiLe
font files.

- Copyright: 2018 The ZCOOL KuaiLe Project Authors
- License: SIL Open Font License 1.1
- License copy: `third_party_licenses/ZCOOL-KuaiLe-OFL-1.1.txt`

The bundled font remains under OFL-1.1; YawnBot's Apache-2.0 license does not
replace the font's license.

## nonebot-plugin-htmlkit 0.1.0rc5

YawnBot redistributes `nonebot-plugin-htmlkit==0.1.0rc5` in its published Python
runtime environment. Upstream documents the Python package code as MIT and the
native `core` extension as LGPL-3.0-or-later.

The published wheel contains `nonebot_plugin_htmlkit/core.abi3.so`, while its
wheel license metadata includes only the root MIT license. YawnBot therefore
adds the missing LGPL-3.0 and GPL-3.0 license texts to the container image under
`/app/third_party_licenses/` from Debian's standard common-license files.

- Python license copy: `third_party_licenses/nonebot-plugin-htmlkit-MIT.txt`
- Native core license: LGPL-3.0-or-later
- Exact upstream source archive: `nonebot_plugin_htmlkit-0.1.0rc5.tar.gz`
- Source archive SHA256: `5c9fc3ed1d1cbf95711006761d19e7a1dc0d0e8b7989c2e806a2bff3aeff7b17`
- Upstream source repository: `https://github.com/nonebot/plugin-htmlkit`

The source distribution records its litehtml submodule at commit
`2fcc6f567cca06d6682ffab3868632c4d9fcc673`.

## litehtml

The HTMLKit native renderer includes litehtml at the exact commit listed above.

- Copyright: 2013 Yuri Kobets (tordex)
- License: BSD-3-Clause
- License copy: `third_party_licenses/litehtml-BSD-3-Clause.txt`

## text-unidecode

`text-unidecode` is a transitive runtime dependency. Upstream offers it under
either GPL/GPLv2+ or the Artistic License. YawnBot redistributes the unmodified
package under the Artistic License option supplied by upstream; the installed
package's own license file remains in its Python distribution metadata.

## certifi and tqdm

These runtime packages expose MPL-related license metadata (`certifi`: MPL-2.0;
`tqdm`: MPL-2.0 AND MIT). Their installed license files remain in the Python
package metadata copied into `/app/.venv`. YawnBot does not modify or relicense
those packages.

## Playwright browser payload

The runtime image contains the browser payload installed by Playwright under
`/ms-playwright`. YawnBot does not strip the upstream license/credits files.
The verified Playwright 1.62.0 payload includes, among other notices:

- Chrome Headless Shell `LICENSE.headless_shell`
- FFmpeg `COPYING.LGPLv2.1`
- Widevine CDM `LICENSE`

See `third_party_licenses/README.md` for the redistribution layout and source
traceability notes.
