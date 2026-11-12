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
run the downloaded exe from anywhere. A new service keeps its data in
`C:\ProgramData\ser2net\` unless you pass `--data-dir`.

Both folders end up owned by *Administrators*, with only SYSTEM and Administrators able to
change them (Users may read and run the program) — not even your own account outside an
elevated prompt, since a program you run unelevated must not be able to change what the
service runs as SYSTEM. So run ser2net against the service's data directory only from an
elevated prompt, with the service stopped (or `--set-password`, also while it runs); it
leaves the folder's permissions as they are. A new service won't take over a data folder
that holds files owned by another account (a standard user can create folders in
`C:\ProgramData`): check and delete it, or take it over, and install again.

## In a console window or as a service

The exe also runs in a console window (double-click, or from a prompt), which is handy to
try it out. For production install the service: it starts at boot and needs nobody signed
in. In a console window, a click into it (QuickEdit mode, on by default) or *Edit → Mark*
pauses the window's output until you press Esc or Enter; ser2net keeps forwarding
meanwhile, only its console lines wait (and past a limit are dropped and counted, the next
line says how many); `all.log` in the data directory has every line.

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

### When the service doesn't start

Look in the data directory (`C:\ProgramData\ser2net\` by default):

- `all.log` — ser2net's log, with the reason a start failed (a `config.json` that can't be
  read, an admin UI address that is in use or not on this machine, a TLS file that can't
  be loaded).
- `service.log` — what Python itself reports (a traceback no handler took, a crash
  dump), begun anew at each start; the previous start's is `service.log.1`.
- *Event Viewer → Windows Logs → Application*, source `ser2net` (the service name): one
  error entry with the reason when the service stops on an error. (The source is not
  registered, so Event Viewer first says it can't find a description for event 1; the
  reason follows.)

### Upgrading the service

Download the new `ser2net-windows-x64.exe` and, from an elevated prompt, run
`--install-service` again (same `--service-name`):

```bat
ser2net-windows-x64.exe --install-service
```

It stops the service, replaces the copy in `C:\Program Files\ser2net\`, and starts it
again — **in place**, keeping the service name and the data directory the service uses
(read from its registration, so leave out `--data-dir`). If the new version doesn't start,
the previous one is put back and started again. This also upgrades a service installed by
ser2net 2.8.0 that still runs from a folder like `C:\ser2net\`: from then on it runs from
`C:\Program Files\ser2net\` and keeps its data where it was (`C:\ser2net\data`).

When other accounts can rename a folder above that data directory (as with a folder made at
the root of `C:\`), the upgrade warns: they could swap it for one of their own. Move it to
`C:\ProgramData\ser2net`: `sc stop ser2net`, copy the folder there, then
`--install-service --data-dir C:\ProgramData\ser2net`; keep the old folder until the
service works. (`--data-dir` pointing to a folder without a `config.json` while the
current one has one is refused, so an upgrade can't start over empty.)

### Installing from a source checkout

A source checkout can't copy itself to an admin-only folder, so `--install-service`
refuses while an account other than SYSTEM and Administrators can change the interpreter,
the checkout, `app\` or `lib\` — your own account included when it owns the folder. Put
the checkout and Python under `C:\Program Files\` (or another admin-only folder) owned by
Administrators; for a folder you already have, from an elevated prompt:

```bat
icacls "C:\Program Files\ser2net-src" /setowner *S-1-5-32-544 /t /c
icacls "C:\Program Files\ser2net-src" /reset /t /c
```

If you trust the location as it is, add `--allow-unsafe-location`; the service then logs a
warning at every start.

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

From an elevated prompt (same `--service-name` if you gave one):

```bat
ser2net-windows-x64.exe --install-service --reconfigure
```

It stops the service, asks for the address on the console, saves it and starts the
service again with it. (Running `--reconfigure` on its own while the service runs is
refused: the data directory allows only one running instance.)

## One instance per data directory

ser2net locks its data directory (`ser2net.lock`) while it runs, so a second instance on
the **same** data directory refuses to start rather than two processes overwriting each
other's `config.json` — a plain `--reconfigure` while the service runs, too.
`--set-password` and `--sign-out-all` (account recovery) are not refused: while the service
runs they hand the change to it, which applies it at once; while it is stopped they take
the lock for the moment they change `config.json`, and a service that starts meanwhile
waits for them.
`--install-service` only checks the lock, after stopping the service it upgrades. To run a
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
