# Linux package repositories (APT, DNF, zypper)

<https://haliskilic.github.io/ser2netweb/> serves signed APT and DNF / zypper repositories with the `.deb` and
`.rpm` packages of ser2netweb's newest three releases (not pre-releases), for x86_64 and
arm64. They are the release files as they are, the ones on GitHub Releases: each is checked
against its release's `SHA256SUMS` and build provenance before it goes in, and nothing is
rebuilt. With the repository added once, the package manager installs the updates with
the rest of the system. What the package installs (the binary, its systemd unit, the
account `ser2netweb` and `/var/lib/ser2netweb`) is in the README's
[*Linux packages*](../README.en.md#linux-packages-deb--rpm).

- Debian 11+, Ubuntu 20.04+, 64-bit Raspberry Pi OS: amd64, arm64 (APT)
- Fedora, RHEL / AlmaLinux / Rocky Linux 8+: x86_64, aarch64 (DNF)
- openSUSE Leap 15.6 and newer: x86_64, aarch64 (zypper)

The commands below need `curl` (Debian / Ubuntu: `sudo apt install curl ca-certificates`).

## Debian, Ubuntu, Raspberry Pi OS (APT)
```bash
sudo curl -fsSLo /usr/share/keyrings/ser2netweb-archive-keyring.gpg https://haliskilic.github.io/ser2netweb/ser2netweb-archive-keyring.gpg
sudo curl -fsSLo /etc/apt/sources.list.d/ser2netweb.sources https://haliskilic.github.io/ser2netweb/ser2netweb.sources
sudo apt update
sudo apt install ser2netweb
```
The first command puts the [signing key](#the-signing-key) into
`/usr/share/keyrings/ser2netweb-archive-keyring.gpg`, the second adds `/etc/apt/sources.list.d/ser2netweb.sources`:
```
Types: deb
URIs: https://haliskilic.github.io/ser2netweb/deb
Suites: stable
Components: main
Architectures: amd64 arm64
Signed-By: /usr/share/keyrings/ser2netweb-archive-keyring.gpg
```
`Signed-By` makes APT take this repository's indexes only when this key signed them, and
this key counts for no other repository.

## Fedora, RHEL, AlmaLinux, Rocky (DNF)
```bash
sudo curl -fsSLo /etc/yum.repos.d/ser2netweb.repo https://haliskilic.github.io/ser2netweb/ser2netweb.repo
sudo dnf install ser2netweb
```
This adds `/etc/yum.repos.d/ser2netweb.repo` (`baseurl=https://haliskilic.github.io/ser2netweb/rpm/$basearch`,
`repo_gpgcheck=1`, `gpgcheck=0`, see [below](#why-the-rpm-packages-are-checked-through-the-signed-metadata)).
On the first install dnf fetches the key named by `gpgkey=` and asks before it imports it
(`Importing GPG key 0x711BB799` … `Fingerprint: B587 60E7 C0B0 515E 81D2  50F9 8164 D694 711B B799`): answer `y` only when it shows this
fingerprint.

## openSUSE (zypper)
```bash
sudo rpm --import https://haliskilic.github.io/ser2netweb/ser2netweb-archive-keyring.asc
sudo zypper addrepo https://haliskilic.github.io/ser2netweb/ser2netweb.repo
sudo zypper refresh
sudo zypper install ser2netweb
```
`rpm --import` makes zypper trust the key (`rpm -qa gpg-pubkey --qf '%{VERSION} %{SUMMARY}\n'`
lists the trusted keys; this one is "ser2netweb package signing"); `zypper addrepo` reads
the same `ser2netweb.repo` as dnf.

## Then
Installing doesn't start the service: `sudo systemctl enable --now ser2netweb` does, and
the web UI then listens on `127.0.0.1:8080`; `sudo -u ser2netweb ser2netweb --set-password admin`
sets the first password. For the network address and the rest, see the README's *Run as a
service*.

## The signing key
`InRelease` and `Release.gpg` (APT) and every `repodata/repomd.xml.asc` (DNF, zypper) are
signed with

    B587 60E7 C0B0 515E 81D2  50F9 8164 D694 711B B799

(`B58760E7C0B0515E81D250F98164D694711BB799`), RSA 4096, "ser2netweb package signing
(https://github.com/haliskilic/ser2netweb)", valid until 2031-11-12. It signs these
repositories and nothing else. The key: <https://haliskilic.github.io/ser2netweb/ser2netweb-archive-keyring.asc>, dearmored for
APT's `Signed-By`: <https://haliskilic.github.io/ser2netweb/ser2netweb-archive-keyring.gpg>; the same file is in this repository
as [`packaging/ser2netweb-archive-keyring.asc`](../packaging/ser2netweb-archive-keyring.asc). To check the one you
have:
```bash
gpg --show-keys /usr/share/keyrings/ser2netweb-archive-keyring.gpg
```
Before it expires, the key gets a later expiry date (the same key and fingerprint); APT
users then fetch it again with the first install command, dnf and zypper users re-import
it (`sudo rpm --import https://haliskilic.github.io/ser2netweb/ser2netweb-archive-keyring.asc`).

## Updating
```bash
sudo apt update
sudo apt install --only-upgrade ser2netweb
```
(or `sudo apt update && sudo apt upgrade`, everything at once)
```bash
sudo dnf upgrade --refresh ser2netweb
```
```bash
sudo zypper refresh
sudo zypper update ser2netweb
```
A newer package restarts a running service. The repository holds the newest three
releases, so an older one of them can still be installed: `sudo apt install ser2netweb=X.Y.Z`
(`--allow-downgrades` to go back), `sudo dnf downgrade ser2netweb-X.Y.Z`,
`sudo zypper install --oldpackage ser2netweb-X.Y.Z`.

## Removing
```bash
sudo apt remove ser2netweb
sudo rm /etc/apt/sources.list.d/ser2netweb.sources /usr/share/keyrings/ser2netweb-archive-keyring.gpg
```
```bash
sudo dnf remove ser2netweb
sudo rm /etc/yum.repos.d/ser2netweb.repo
```
```bash
sudo zypper remove ser2netweb
sudo zypper removerepo ser2netweb
```
Removing the package stops and disables the service and keeps `/var/lib/ser2netweb` and the
account `ser2netweb` (delete them by hand if wanted). A key that `rpm --import` (or dnf)
put into the RPM database stays there: `rpm -qa gpg-pubkey --qf '%{NAME}-%{VERSION}-%{RELEASE} %{SUMMARY}\n'`
names it, `sudo rpm -e <that name>` removes it.

## Why the RPM packages are checked through the signed metadata
The `.rpm` files are released unsigned (like the `.deb` files: Debian packages carry no
signature of their own either). What dnf and zypper check is the repository:
`repo_gpgcheck=1` makes them verify `repodata/repomd.xml` against `repomd.xml.asc` with
the key above before they use it, `repomd.xml` holds the SHA-256 of the package list
(`primary.xml.gz`), and that list holds the SHA-256 and size of every package, which dnf
and zypper compare with what they download. A package that was changed on the server or on
the way doesn't match, and a changed list doesn't match the signed `repomd.xml`
(the workflow tests both). `gpgcheck=0` only tells them not to look for a signature inside
the package file, which isn't there. APT works the same way: `InRelease` is signed and
holds the SHA-256 of `Packages`, which holds each package's.

A `.rpm` (or `.deb`) copied around outside the repository has none of this: compare it
with the release's `SHA256SUMS`, or check its build provenance
(`gh attestation verify ser2netweb-X.Y.Z-1.x86_64.rpm -R haliskilic/ser2netweb`). Signing the
RPM files themselves when they are built (nfpm's `rpm.signature`) would let `gpgcheck=1`
check each file too; that is a later step.

## How the repositories are built (maintainers)
`.github/workflows/package-repo.yml` runs `tools/package_repo.py`:
1. **fetch**: the `.deb` / `.rpm` files of the newest three published releases (by
   version; pre-releases and drafts never), each checked against its release's
   `SHA256SUMS` and the digest GitHub lists, then their build provenance
   (`gh attestation verify --signer-workflow …/release.yml`);
2. **build**: `deb/` with `pool/main/s/ser2netweb/` and
   `dists/stable/main/binary-{amd64,arm64}/Packages(.gz)` (apt-ftparchive, SHA-256 only,
   also under `by-hash/`, so a cached older index can't be mixed with a newer `InRelease`)
   and `dists/stable/Release`; `rpm/x86_64/` and `rpm/aarch64/` with `repodata/`
   (createrepo_c, gzip, no sqlite); `ser2netweb.sources`, `ser2netweb.repo`, `index.html`;
3. **sign**: `InRelease`, `Release.gpg`, `repomd.xml.asc` (SHA-512) with the key in the
   repository secrets `PACKAGES_GPG_PRIVATE_KEY` / `PACKAGES_GPG_PASSPHRASE` (only that
   step gets them; their fingerprint must be the one above, the committed
   `packaging/ser2netweb-archive-keyring.asc`'s and `PACKAGES_GPG_FINGERPRINT`'s), in a temporary
   GnuPG home deleted afterwards; the published keyring is the committed file;
4. **verify**: every index against the files it lists, every signature with `gpgv`;
5. **test**: `tests/package_repo_smoke.sh` serves the tree on 127.0.0.1 and, in clean
   Debian 12, Ubuntu 24.04, Fedora, AlmaLinux 9 and openSUSE Leap 15.6 containers (x64;
   Debian and Fedora on arm64 too), runs the commands above (`tools/package_repo.py
   commands`): a copy whose `InRelease` and `repomd.xml` were changed after signing must be
   refused, then ser2netweb is installed, `ser2netweb --version` checked, updated and
   removed;
6. **deploy** to GitHub Pages (the environment `github-pages`, which allows `main` and the
   `v*` tags), then the same commands against the published site.

It runs when release.yml has published a release (its job *start the release channels*
starts it on `main`: GitHub starts no workflow for a release its own token created), for a
release published by hand, and from *Actions → Package repositories → Run workflow*, whose
`tag` builds the repositories as of that release (it and the two before it; a rollback).
A pull request that changes the workflow, `packaging/`, the tool or the smoke test builds
and tests a tree signed with a throwaway key made in the job and deploys nothing; so does
a run without `PACKAGES_GPG_PRIVATE_KEY` (with a notice). Every run replaces the whole
site, so anything else on GitHub Pages would have to be built here too.

Each release adds about 95 MB (four packages); three releases stay well inside GitHub
Pages' 1 GB site limit (`KEEP` in the tool). Locally, with apt-ftparchive and createrepo_c
on `PATH`: `python3 tools/package_repo.py fetch dl`, `build dl site`,
`sign site --test-key`, `verify site --any-key`; the smoke test needs Docker.

Not here (yet): the distributions' own repositories (Debian, Ubuntu, Fedora: a long review
with sponsors and source builds) and Flatpak (a desktop sandbox, not for a system service).
