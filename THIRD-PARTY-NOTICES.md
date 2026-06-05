# Third-Party Notices

pyser2net bundles the following third-party components (Python wheels in
`vendor/wheels/` and vendored browser assets in `app/web/static/`). Each remains
governed by its own license. All are **permissive** open-source licenses that allow
commercial redistribution provided their copyright/license notices are preserved.

These notices satisfy clause 5 of the pyser2net LICENSE.

## Python dependencies (vendor/wheels/)

| Component | License |
|---|---|
| pyserial | BSD-3-Clause |
| pyserial-asyncio-fast | BSD-3-Clause |
| starlette | BSD-3-Clause |
| uvicorn | BSD-3-Clause |
| websockets | BSD-3-Clause |
| python-multipart | Apache-2.0 |
| Jinja2 | BSD-3-Clause |
| MarkupSafe | BSD-3-Clause |
| anyio | MIT |
| sniffio | MIT / Apache-2.0 |
| click | BSD-3-Clause |
| h11 | MIT |
| idna | BSD-3-Clause |
| typing_extensions | PSF |
| psutil | BSD-3-Clause |

Optional (not bundled by default): `pyudev` (LGPL-2.1, used as a separate library via
its public API on Linux), `pywin32` (PSF-style, Windows).

## Browser assets (app/web/static/)

| Component | License |
|---|---|
| htmx | 0BSD |
| xterm.js (xterm.js, xterm.css) | MIT |
| xterm-addon-fit | MIT |

---

Full license texts are available from each project's distribution and homepage.
Preserve this file when redistributing pyser2net.
