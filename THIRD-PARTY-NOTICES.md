# Third-Party Notices

ser2net bundles the following third-party components (Python wheels in
`vendor/wheels/` and vendored browser assets in `app/web/static/`). Each remains
governed by its own license. All are **permissive** open-source licenses that allow
commercial redistribution provided their copyright/license notices are preserved.

These notices satisfy clause 5 of the ser2net LICENSE.

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

### Optional feature dependencies

Not installed by the offline bootstrap, but used when the corresponding feature is
enabled and **bundled into the standalone binary and the Docker image**. Each is used
as a library via its public API (no source modification), and shipped unmodified:

| Component | Feature | License |
|---|---|---|
| paho-mqtt | MQTT publishing | EPL-2.0 / EDL-1.0 (dual) |
| ldap3 | LDAP / AD auth | **LGPL-3.0** (dynamically linked library; replaceable) |
| pyasn1 (ldap3 dep) | LDAP / AD auth | BSD-2-Clause |
| Authlib | OIDC SSO | BSD-3-Clause |
| cryptography (Authlib dep) | OIDC SSO | Apache-2.0 OR BSD-3-Clause |
| pyudev | hotplug (Linux) | LGPL-2.1 (library via public API) |
| pywin32 | hotplug (Windows) | PSF-style |

> ldap3 and pyudev are LGPL: they are used unmodified as separate, replaceable
> libraries through their public APIs, which their licenses permit in a proprietary
> product. To honor the LGPL's relinking right, the standalone binary's ldap3/pyudev
> can be swapped by installing a different version alongside it.

## Browser assets (app/web/static/)

| Component | License |
|---|---|
| htmx | 0BSD |
| xterm.js (xterm.js, xterm.css) | MIT |
| xterm-addon-fit | MIT |

---

Full license texts are available from each project's distribution and homepage.
Preserve this file when redistributing ser2net.
