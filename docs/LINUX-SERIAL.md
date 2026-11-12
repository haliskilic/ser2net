# Serial ports on Linux: troubleshooting

Most problems with a serial port on Linux are not ser2net's: the account has no access to
the device, another program uses the port, or the port has another name than expected.
Each is fixed with one group change, one udev rule or one setting. Start from what the
dashboard shows under the mapping's state.

## What the dashboard says

The mapping's state and its hint (also `hint` and `last_error` in `GET /api/v1/status`, and
the mapping's log) say what went wrong; `last_error` is pyserial's own text.

| Shown | Cause | Fix |
|---|---|---|
| `[Errno 13] Permission denied`, with *… belongs to group dialout (gid 20), and user ser2net is not in it* | the account ser2net runs as is not in the device's group | [Groups](#groups) |
| … *was added to group … after ser2net started* | the group was given while ser2net ran | restart ser2net (`sudo systemctl restart ser2net`) |
| … *only root may open …* | the device belongs to root's group | a udev rule that gives it the serial group ([udev](#udev-rules)) |
| `[Errno 16] Device or resource busy`, or *another program keeps other programs out of this port (TIOCEXCL)* | another program opened it exclusively: ModemManager, the classic ser2net, brltty | [Who has the port](#who-has-the-port) |
| `Could not exclusively lock port …`, *another program has locked this port (flock)* | another ser2net mapping or instance, picocom, tio, miniterm | stop that program; *mapping '…' has this port open* names this ser2net's own |
| *… (PID …) is using this port: it holds a UUCP lock file on it* | minicom, cu (with **Don't open a port another program has locked with a UUCP lock file** on) | close that program |
| state `device missing`, `[Errno 2] No such file or directory` | the device is not there: unplugged, not enumerated yet, another name after re-plugging | [Device names](#device-names) |
| *is also the kernel console*, *a login prompt runs on this port* | the kernel or a getty uses the port | [Console and login prompt](#console-and-login-prompt) |
| *ModemManager may probe …* | ModemManager sends AT commands to a new `ttyACM*` | [ModemManager](#modemmanager) |
| *hung up N times while open, and could be reopened at once* | another program (usually a login prompt) hangs the port up | [Console and login prompt](#console-and-login-prompt) |
| *RS-485 mode is off: the port did not take it*, *RTS could not be switched on* | the adapter or driver has no RS-485 mode or modem lines (most USB adapters) | use an adapter with automatic direction control, or untick RS-485 |
| garbage, Modbus timeouts on a Raspberry Pi GPIO UART with parity | the mini UART has no parity | [Raspberry Pi](#raspberry-pi) |

The **Detected serial ports** panel marks ports with **kernel console**, **login prompt**,
**ModemManager** and **no access** before a mapping uses them.

## Groups

A serial device belongs to a group, and only its members (and root) may open it. Which
group: ask the device.

```bash
stat -L -c '%G (gid %g)' /dev/ttyUSB0      # dialout (gid 20) on Debian/Ubuntu, dialout (18) on Fedora/RHEL, uucp on Arch
sudo usermod -aG "$(stat -L -c %G /dev/ttyUSB0)" "$USER"     # then sign out and in again
```

- **systemd** (`systemd/ser2net.service`, `ser2net@.service`, `ser2net-standalone.service`):
  the unit gives the account the group with `SupplementaryGroups=dialout`. On a
  distribution whose serial group has another name (Arch, Manjaro, EndeavourOS: `uucp`) the
  unit fails with `status=216/GROUP`: set it with `sudo systemctl edit ser2net`:
  ```ini
  [Service]
  SupplementaryGroups=
  SupplementaryGroups=uucp
  ```
- **Docker:** the entrypoint gives ser2net the group of each device passed with `--device`
  under a usual serial name, from the device itself, so no `--group-add` is needed; a device
  mapped to another name inside the container needs
  `--group-add "$(stat -L -c %g /dev/ttyUSB0)"` (the numeric GID: the name `dialout`
  resolves to the image's 20, not the host's). See [`DOCKER.md`](DOCKER.md).
- Never run ser2net as root to get around a missing group.

## udev rules

A rule in `/etc/udev/rules.d/` sets a device's group, mode or a fixed name when it
appears. Find an adapter's attributes with `udevadm info -a -n /dev/ttyUSB0`.

```text
# /etc/udev/rules.d/99-serial.rules
# a device that comes up as root:root: give it the serial group
SUBSYSTEM=="tty", KERNEL=="ttyAMA0", GROUP="dialout", MODE="0660"
# a fixed name for one adapter (by its serial number)
SUBSYSTEM=="tty", ATTRS{idVendor}=="0403", ATTRS{idProduct}=="6001", ATTRS{serial}=="A1B2C3", SYMLINK+="plc1"
```

Then `sudo udevadm control --reload && sudo udevadm trigger`. A `SYMLINK` name
(`/dev/plc1`) can be typed into the mapping form's **Device** field (*Other device path…*).

## Device names

- `ttyUSB*` (FTDI, CH340, CP210x, PL2303) and `ttyACM*` (USB CDC: Arduino, many modems) are
  numbered in the order they appear, so after re-plugging or a reboot two adapters can swap.
  Use one of:
  - **`/dev/serial/by-id/…`** (in the Device list under *By serial number*): follows the
    adapter wherever it is plugged in, but two identical adapters without a serial number
    of their own get the same name (marked *not unique*);
  - **`/dev/serial/by-path/…`** (*By USB socket*): right while the adapter stays in the
    same socket;
  - **Find this device by its USB identity if its name changes** (VID, PID and serial
    number): ser2net re-finds the adapter after renumbering, and shows `device missing`
    instead of opening another adapter that took its name.
- On-board UARTs are `ttyS*` (PC), `ttyAMA*` (Raspberry Pi, other ARM boards),
  `ttymxc*` (i.MX), `ttyTHS*` (Jetson), `ttyPS*` (Zynq): the Device list shows every port a
  kernel serial driver registers, and any other path can be typed.

## Who has the port

```bash
sudo fuser -v /dev/ttyUSB0           # processes that have it open
sudo lsof /dev/ttyUSB0
ls -l /run/lock/LCK..* /var/lock/LCK..* 2>/dev/null   # UUCP lock files (minicom, cu)
cat /proc/locks                      # flock()s; the inode is in `stat -L -c %i /dev/ttyUSB0`
```

What ser2net's own locking does (mapping form → *Keeping other programs off the port*):
**Lock the port (flock)**, on by default, keeps out other ser2net mappings and programs
that lock the same way (picocom, tio, miniterm), not minicom, `cat`, `stty`, scripts, a
login prompt or ModemManager. **Keep other programs out while the port is open**
(`TIOCEXCL`) makes any other program's open fail as busy, except root's. **Don't open a
port another program has locked with a UUCP lock file** honours minicom's and cu's `LCK..`
files (ser2net makes none). Inside a container a `--device` port is a device node of its
own, so a lock there may not keep host programs out.

## Console and login prompt

A port that is also the kernel console gets the kernel's messages, and a login prompt
(`serial-getty@ttyS0.service`) reads from it and hangs it up whenever it restarts.

- Raspberry Pi: `sudo raspi-config` → *Interface Options* → *Serial Port*: *No* to the
  login shell, *Yes* to the serial hardware; reboot.
- Elsewhere: remove `console=ttyS0,…` from the kernel command line (GRUB:
  `GRUB_CMDLINE_LINUX` in `/etc/default/grub`, then `sudo update-grub` or
  `sudo grub2-mkconfig -o /boot/grub2/grub.cfg`), and stop the login prompt:
  `sudo systemctl mask --now serial-getty@ttyS0.service`.

## ModemManager

ModemManager (desktop Ubuntu and Fedora) probes new modem-like ports with AT commands,
mainly USB CDC `ttyACM*` devices; it leaves ordinary USB-serial adapters (FTDI, CH340,
`ttyUSB*`) alone by default. Tell it to ignore a device with a udev rule:

```text
# /etc/udev/rules.d/99-ser2net-mm.rules (VID/PID from `udevadm info -a -n /dev/ttyACM0`)
ATTRS{idVendor}=="2341", ENV{ID_MM_DEVICE_IGNORE}="1"
```

then `sudo udevadm control --reload && sudo udevadm trigger` and re-plug the device. On a
headless gateway with no modem, `sudo systemctl disable --now ModemManager` does too.

## brltty

brltty, the braille display daemon (installed by default on some desktop distributions,
Ubuntu 22.04 among them), claims some USB-serial adapters (CH340/CH341 among them) as
braille displays: the `ttyUSB*` device appears and is gone at once. If no braille display
is used: `sudo apt remove brltty`, or `sudo systemctl mask brltty-udev.service`, then
re-plug the adapter. `journalctl -k` shows `usbfs: interface 0 claimed by ch341 while
'brltty' sets config` when it happens.

## Raspberry Pi

- The GPIO header's UART (pins 8/10) is `/dev/serial0`, a link to the UART the firmware
  puts there: on a Pi 3, 4 or Zero W the **mini UART** `ttyS0` (the full PL011 UART
  `ttyAMA0` drives Bluetooth); on a Pi 1, 2 or Zero `ttyAMA0`. `ls -l /dev/serial0` says
  which. Type `/dev/serial0` into the Device field to keep the mapping right when the UARTs
  are swapped.
- The **mini UART has no parity** and its baud rate follows the core clock. Modbus RTU
  with even parity (8E1) needs the PL011: add `dtoverlay=disable-bt` (Bluetooth off) or
  `dtoverlay=miniuart-bt` (Bluetooth on the mini UART) to `config.txt`
  (`/boot/firmware/config.txt` on current Raspberry Pi OS), reboot, and
  `/dev/serial0` then points at `ttyAMA0`.
- The Pi 5 has a PL011 on the GPIO header and a separate debug UART connector
  (`ttyAMA10`), with no Bluetooth swap; check `ls -l /dev/serial0` there too.
- Turn the serial console off first ([above](#console-and-login-prompt)).
- The standalone `ser2net-linux-arm64` needs a 64-bit OS; 32-bit Raspberry Pi OS runs from
  source or with the Docker image (`linux/arm/v7`).

## Still stuck

Open an issue with the mapping's state and hint from the dashboard, the lines of the
mapping's log around the error, `ser2net --about`, and the output of
`ls -l /dev/serial/by-id/ /dev/ttyUSB* /dev/ttyACM* 2>/dev/null` and `id ser2net` (mask
serial numbers you don't want to publish).
