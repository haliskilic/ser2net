# ser2net on Windows

Running the standalone `ser2net-windows-x64.exe`, as a background service or
interactively. The exe bundles everything (Python, OpenSSL, the VC++ runtime, MQTT /
LDAP / OIDC) — no Python install is needed.

## Where to put it

Install the exe where **only administrators can change it**, and keep the data directory
separate:

| | Path | Why |
|---|---|---|
| Program (exe) | `C:\Program Files\ser2net\ser2net.exe` | admin-only, so a standard user can't replace the exe the service runs as SYSTEM |
| Data | `C:\ProgramData\ser2net\` | config, logs, backups, TLS — kept private, writable by the service |

Do **not** run the service from a folder a standard user can write to (for example a
folder you created at the root of `C:\` or `D:\`, or your `Downloads`). On Windows 10/11
those grant *Modify* to every signed-in user, who could then replace the exe and have the
service run their code as SYSTEM.

When you run `--install-service` from the standalone exe, it **copies itself into
`C:\Program Files\ser2net\` for you** and registers the service from there, so you can
run the downloaded exe from anywhere. The data directory defaults to
`C:\ProgramData\ser2net\` unless you pass `--data-dir`.

## Install

From an elevated (**Run as administrator**) Command Prompt or PowerShell:

```bat
ser2net-windows-x64.exe --install-service
```

The service starts at boot as LocalSystem, restarts 5 s after a failure, and the console
asks once for the admin UI address. Then open the printed URL and set the admin password.

Manage it with `sc stop ser2net` / `sc start ser2net`, or *Services* (`services.msc`).
Run several instances side by side with `--service-name NAME` (each needs its own
`--data-dir`). Remove one with `--uninstall-service` (the data directory is kept):

```bat
ser2net-windows-x64.exe --uninstall-service
```

### Upgrading the service

Download the new `ser2net-windows-x64.exe` and, from an elevated prompt, run
`--install-service` again (same `--service-name`):

```bat
ser2net-windows-x64.exe --install-service
```

It stops the service, replaces the copy in `C:\Program Files\ser2net\`, and starts it
again — **in place**, keeping the service name and data directory. (Doing it by hand
works too: `sc stop ser2net`, replace the exe, `sc start ser2net`.)

### Installing from a source checkout

A source checkout can't copy itself to an admin-only folder. Put the checkout under
`C:\Program Files\` (or another admin-only folder), or, if you trust the location, add
`--allow-unsafe-location`. The service logs a warning at start-up while it runs from a
place a non-administrator can modify.

## Windows Firewall

A LocalSystem service never gets the interactive "allow this app" prompt, so until you
add a rule the firewall blocks **other hosts** from reaching the admin UI (when it is
bound to a LAN address), the serial mapping ports (they listen on `0.0.0.0` by default)
and the cluster's UDP discovery port (41750). Local access on the same machine still
works, which is why this isn't obvious at first.

`--install-service` prints the exact command when anything listens beyond loopback. One
program-scoped rule covers the admin UI, every mapping port and the cluster:

```bat
netsh advfirewall firewall add rule name="ser2net" dir=in action=allow program="C:\Program Files\ser2net\ser2net.exe" enable=yes
```

Remove it with:

```bat
netsh advfirewall firewall delete rule name="ser2net"
```

ser2net does not open the firewall for you (secure by default). Prefer binding the admin
UI to `127.0.0.1` and reaching it over SSH / a reverse proxy if you don't need it on the
LAN; the mapping ports still need the rule to be reachable from other hosts.

## Changing the admin UI address

Stop the service first (the data directory allows only one running instance), then run
the picker, which saves and starts a foreground server; stop it with Ctrl-C and start the
service again:

```bat
sc stop ser2net
ser2net-windows-x64.exe --reconfigure --data-dir C:\ProgramData\ser2net
sc start ser2net
```

## One instance per data directory

ser2net locks its data directory (`ser2net.lock`) while it runs, so a second instance on
the **same** data directory refuses to start rather than two processes overwriting each
other's `config.json`. That is why `--reconfigure` must be run after `sc stop`. To run a
genuinely separate instance, give it its own `--data-dir` (and `--service-name`).

## Unsigned builds and SmartScreen

Releases are **not code-signed yet**, so SmartScreen may warn ("Windows protected your
PC") the first time you run the exe, and some antivirus products flag it more readily.
Verify the download before trusting it:

- **Checksum** (PowerShell) — compare with the `SHA256SUMS` file attached to the release:
  ```powershell
  Get-FileHash .\ser2net-windows-x64.exe -Algorithm SHA256
  ```
  (`sha256sum` is not present on Windows by default; use `Get-FileHash`.)
- **Build provenance** (needs the GitHub CLI) — proves GitHub Actions built it from this
  repository:
  ```powershell
  gh attestation verify .\ser2net-windows-x64.exe -R haliskilic/ser2net
  ```

Once verified, run it past SmartScreen with **More info → Run anyway**. Signing will be
added when a signing service is set up (see [`SIGNING.md`](SIGNING.md)); this section will
shrink then.
