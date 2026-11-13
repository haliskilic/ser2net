# ser2netweb on Windows

Running the standalone `ser2netweb-windows-x64.exe`, as a background service or
interactively. The exe bundles everything (Python, OpenSSL, the VC++ runtime, MQTT /
LDAP / OIDC) — no Python install is needed.

## Where to put it

Install the exe where **only administrators can change it**, and keep the data directory
separate:

| | Path | Why |
|---|---|---|
| Program (exe) | `C:\Program Files\ser2netweb\ser2netweb.exe` | admin-only, so a standard user can't replace the exe the service runs as SYSTEM |
| Data | `C:\ProgramData\ser2netweb\` | config, logs, backups, TLS — kept private, writable by the service |

(Up to 2.9.x the service, the folders and the exe were called `ser2net`: *Upgrading from 2.x*
below.)

Do **not** run the service from a folder a standard user can write to (for example a
folder you created at the root of `C:\` or `D:\`, or your `Downloads`). On Windows 10/11
those grant *Modify* to every signed-in user, who could then replace the exe and have the
service run their code as SYSTEM.

When you run `--install-service` from the standalone exe, it **copies itself into
`C:\Program Files\ser2netweb\` for you** and registers the service from there, so you can
run the downloaded exe from anywhere. A new service keeps its data in
`C:\ProgramData\ser2netweb\` unless you pass `--data-dir`.

A pip / pipx install (`pipx install ser2netweb`, PyPI) can't be a service: its Python and
packages are the user's to change (a pipx environment lives in the user profile), so
`--install-service` refuses there. It runs in a console window as below, with its data in
`%LOCALAPPDATA%\ser2netweb\`; for a service take the standalone exe.

Both folders end up owned by *Administrators*, with only SYSTEM and Administrators able to
change them (Users may read and run the program) — not even your own account outside an
elevated prompt, since a program you run unelevated must not be able to change what the
service runs as SYSTEM. So run ser2netweb against the service's data directory only from an
elevated prompt, with the service stopped (or `--set-password`, also while it runs); it
leaves the folder's permissions as they are. A new service won't take over a data folder
that holds files owned by another account (a standard user can create folders in
`C:\ProgramData`): check and delete it, or take it over, and install again.

## In a console window or as a service

The exe also runs in a console window (double-click, or from a prompt), which is handy to
try it out. For production install the service: it starts at boot and needs nobody signed
in. In a console window, a click into it (QuickEdit mode, on by default) or *Edit → Mark*
pauses the window's output until you press Esc or Enter; ser2netweb keeps forwarding
meanwhile, only its console lines wait (and past a limit are dropped and counted, the next
line says how many); `all.log` in the data directory has every line.

## Install

From an elevated (**Run as administrator**) Command Prompt or PowerShell:

```bat
ser2netweb-windows-x64.exe --install-service
```

The service starts at boot as LocalSystem, restarts 5 s after a failure, and the console
asks once for the admin UI address. Then open the printed URL and set the admin password.

Manage it with `sc stop ser2netweb` / `sc start ser2netweb`, or *Services*
(`services.msc`, where it is listed as *ser2netweb*). Run several instances side by side
with `--service-name NAME`: from the standalone exe a named service gets
`C:\Program Files\ser2netweb\NAME.exe` and `C:\ProgramData\ser2netweb\NAME` by itself
(from a source checkout give each its own `--data-dir`). Remove one with
`--uninstall-service` (the data directory is kept; without a ser2netweb service it removes
2.x's `ser2net` one):

```bat
ser2netweb-windows-x64.exe --uninstall-service
```

### When the service doesn't start

The service is *Starting* until its mappings were started and its admin UI listens, and
*Running* from then on, so `--install-service` waits for that and reports a service that
stops while it starts (its exit code, and where to look) instead of success. A service
whose event loop is stuck for 90 s writes `hang.log` and exits, and is started again 5 s
later (`SER2NETWEB_LOOP_WATCHDOG`, a system environment variable, sets the limit; `0` = off).

Look in the data directory (`C:\ProgramData\ser2netweb\` by default):

- `all.log` — ser2netweb's log, with the reason a start failed (a `config.json` that can't be
  read, an admin UI address that is in use or not on this machine, a TLS file that can't
  be loaded).
- `service.log` — what Python itself reports (a traceback no handler took, a crash
  dump), begun anew at each start; the previous start's is `service.log.1`.
- *Event Viewer → Windows Logs → Application*, source `ser2netweb` (the service name;
  `ser2net` for 2.x's service): one
  error entry with the reason when the service stops on an error. (The source is not
  registered, so Event Viewer first says it can't find a description for event 1; the
  reason follows.)

### Upgrading the service

Download the new `ser2netweb-windows-x64.exe` and, from an elevated prompt, run
`--install-service` again (same `--service-name`):

```bat
ser2netweb-windows-x64.exe --install-service
```

It stops the service, replaces the copy in `C:\Program Files\ser2netweb\`, and starts it
again — **in place**, keeping the service name and the data directory the service uses
(read from its registration, so leave out `--data-dir`). If the new version doesn't start,
the previous one is put back and started again.

When other accounts can rename a folder above that data directory (as with a folder made at
the root of `C:\`), the upgrade warns: they could swap it for one of their own. Move it to
`C:\ProgramData\ser2netweb`: `sc stop ser2netweb`, copy the folder there, then
`--install-service --data-dir C:\ProgramData\ser2netweb`; keep the old folder until the
service works. (`--data-dir` pointing to a folder without a `config.json` while the
current one has one is refused, so an upgrade can't start over empty.)

### Upgrading from 2.x (the service called ser2net)

Up to 2.9.x the service was `ser2net`, the exe `C:\Program Files\ser2net\ser2net.exe` and
the data `C:\ProgramData\ser2net\`. Download the new exe and run the same command, from an
elevated prompt, without `--service-name` or `--data-dir`:

```bat
ser2netweb-windows-x64.exe --install-service
```

- It finds the `ser2net` service and checks that it is one ser2net installed (by its
  registered command line: `--service --service-name ser2net --data-dir …`, run by a
  `ser2net*.exe` or `ser2net.py`). A `ser2net` service of another program is left alone.
- It stops it, puts the new exe in `C:\Program Files\ser2netweb\`, installs and starts the
  `ser2netweb` service, and once that one runs deletes the `ser2net` service and its exe
  copy in `C:\Program Files\ser2net\`. If the new service doesn't start, everything is put
  back and the `ser2net` service starts again.
- **Data:** `C:\ProgramData\ser2net\` moves to `C:\ProgramData\ser2netweb\` (one rename, the
  admin-only permissions go along) when that folder doesn't exist yet. A data directory
  anywhere else (a 2.8.0 install's `C:\ser2net\data`, a `--data-dir` of yours) stays where
  it is, and so does `C:\ProgramData\ser2net\` while it also holds the folder of a named
  service (`C:\ProgramData\ser2net\NAME`): upgrade the named services first (next point),
  then the default one.
- **Named services** (`--service-name NAME`, *ser2net (NAME)* in the Services list) keep
  their name: run `--install-service --service-name NAME` for each. Each is upgraded in
  place, to `C:\Program Files\ser2netweb\NAME.exe` and, when its data was in
  `C:\ProgramData\ser2net\NAME`, `C:\ProgramData\ser2netweb\NAME`.
- **Windows Firewall:** a rule names the program, which moved. Add the rule
  `--install-service` prints (below) and delete the old one:
  `netsh advfirewall firewall delete rule name="ser2net"` (a named service's:
  `name="ser2net (NAME)"`).
- Scripts and tools that use the old service name (`sc stop ser2net`, monitoring) need the
  new one. To keep the old name instead, install with `--service-name ser2net`: that
  upgrades the `ser2net` service in place like a named one (its exe and data then move to
  `C:\Program Files\ser2netweb\ser2net.exe` and `C:\ProgramData\ser2netweb\ser2net`).

### Installing from a source checkout

A source checkout can't copy itself to an admin-only folder, so `--install-service`
refuses while an account other than SYSTEM and Administrators can change the interpreter,
the checkout, `ser2netweb\` or `lib\` — your own account included when it owns the folder. Put
the checkout and Python under `C:\Program Files\` (or another admin-only folder) owned by
Administrators; for a folder you already have, from an elevated prompt:

```bat
icacls "C:\Program Files\ser2netweb-src" /setowner *S-1-5-32-544 /t /c
icacls "C:\Program Files\ser2netweb-src" /reset /t /c
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
netsh advfirewall firewall add rule name="ser2netweb" dir=in action=allow program="C:\Program Files\ser2netweb\ser2netweb.exe" enable=yes
```

Remove it with:

```bat
netsh advfirewall firewall delete rule name="ser2netweb"
```

A named service (`--service-name NAME`) needs a rule of its own for its exe,
`C:\Program Files\ser2netweb\NAME.exe`; `--install-service` prints it with the rule name it
uses (`ser2netweb (NAME)`).

Two environment variables change how ser2netweb uses Windows (for the service: system
environment variables, then restart it): `SER2NETWEB_WINDOWS_LOOP=proactor` lifts the
512-socket limit of the default event loop, and `SER2NETWEB_WINDOWS_SERIAL=pyserial` goes back
to pyserial's own serial writes if a virtual COM driver misbehaves with the paced ones
(README → *Run as a service*).

ser2netweb does not open the firewall for you (secure by default). Prefer binding the admin
UI to `127.0.0.1` and reaching it over SSH / a reverse proxy if you don't need it on the
LAN; the mapping ports still need the rule to be reachable from other hosts. Behind a
reverse proxy (IIS ARR, nginx), set **Settings → Reverse proxy** — the service needs no
environment variables for it ([REVERSE-PROXY.md](REVERSE-PROXY.md)).

## Changing the admin UI address

From an elevated prompt (same `--service-name` if you gave one):

```bat
ser2netweb-windows-x64.exe --install-service --reconfigure
```

It stops the service, asks for the address on the console, saves it and starts the
service again with it. (Running `--reconfigure` on its own while the service runs is
refused: the data directory allows only one running instance.)

The service starts at boot without waiting for the network. When the admin UI's address
(or a mapping's) can't be used yet at that point (a LAN IP that DHCP has not assigned yet,
an unplugged cable, a port still held by another program), the service keeps running: the
mappings start, and the admin UI and each such mapping try again on their own, at most
30 s apart, until they can listen (`all.log` says so once, and again when they are up).

## Changing config.json while the service runs

After editing `config.json` (by hand or with a deployment tool), have the running service
read it again — no restart, mappings whose settings didn't change keep their clients:

```bat
"C:\Program Files\ser2netweb\ser2netweb.exe" --reload --data-dir C:\ProgramData\ser2netweb
```

(or Settings → *Configuration backups* → *Reload config.json*). A file that doesn't
validate changes nothing; `all.log` says why. A renewed admin UI certificate at the same
paths is read by a reload too. A new admin UI address needs the reinstall above.

## One instance per data directory

ser2netweb locks its data directory (`ser2net.lock`) while it runs, so a second instance on
the **same** data directory refuses to start rather than two processes overwriting each
other's `config.json` — a plain `--reconfigure` while the service runs, too.
`--set-password`, `--sign-out-all` (account recovery) and `--reload` are not refused: while
the service runs they hand the request to it, which applies it at once; while it is stopped
`--reload` has nothing to do, and the other two take
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
  Get-FileHash .\ser2netweb-windows-x64.exe -Algorithm SHA256
  ```
  (`sha256sum` is not present on Windows by default; use `Get-FileHash`.)
- **Build provenance** (needs the GitHub CLI) — proves GitHub Actions built it from this
  repository:
  ```powershell
  gh attestation verify .\ser2netweb-windows-x64.exe -R haliskilic/ser2netweb
  ```

Once verified, run it past SmartScreen with **More info → Run anyway**. Signing will be
added when a signing service is set up (see [`SIGNING.md`](SIGNING.md)); this section will
shrink then.
