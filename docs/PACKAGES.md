# Linux package repositories (APT, DNF, zypper)

<https://haliskilic.github.io/ser2netweb/> serves signed APT and DNF / zypper repositories with the `.deb` and
`.rpm` packages of ser2netweb's newest three releases (not pre-releases), for x86_64 and
arm64. They are the release files, the ones on GitHub Releases: each is checked against its
release's `SHA256SUMS` and build provenance before it goes in, and nothing is rebuilt; the
`.rpm` files get the signing key's signature added (below). With the repository added
once, the package manager installs the updates with the rest of the system. What the package installs (the binary, its systemd unit, the
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
`repo_gpgcheck=1`, `gpgcheck=1`, `pkg_gpgcheck=1`, see [below](#how-the-rpm-packages-are-checked)).
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
`InRelease` and `Release.gpg` (APT), every `repodata/repomd.xml.asc` (DNF, zypper) and every
`.rpm` file in the repository are signed with

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

## How the RPM packages are checked
Two signatures by the key above, and dnf and zypper check both, as `ser2netweb.repo` tells
them (`repo_gpgcheck=1` the repository's; `gpgcheck=1` the packages' for dnf, and
`pkg_gpgcheck=1` for zypper, which with a good repository signature otherwise skips the
packages'; dnf 5 reads `pkg_gpgcheck` as its name for `gpgcheck`, dnf 4 passes over it):
- the repository's: dnf and zypper verify `repodata/repomd.xml` against `repomd.xml.asc`
  before they use it; `repomd.xml` holds the SHA-256 of the package list (`primary.xml.gz`),
  and that list the SHA-256 and size of every package, which they compare with what they
  download. A package or list changed on the server or on the way doesn't match (the
  workflow tests that a changed copy is refused);
- each package's own: the `.rpm` files carry a signature in their signature header
  (`rpmsign`, RSA/SHA-512), which `rpm` checks before it installs one. A copy downloaded from
  the repository can be checked on its own too, with the key imported
  (`sudo rpm --import https://haliskilic.github.io/ser2netweb/ser2netweb-archive-keyring.asc`):
  `rpm -K ser2netweb-X.Y.Z-1.x86_64.rpm` says `digests signatures OK`, and once installed
  `rpm -q --qf '%{RSAHEADER:pgpsig}\n' ser2netweb` names the key (`Key ID 8164d694711bb799`).
  `zypper repos` shows both checks as `(rp) Yes` in its *GPG Check* column.

The packages are signed when the repository is built, not when they are released: the
signature is added to the release's file, whose header, payload and their digests stay as
they are (`rpm -K` shows the same `Header SHA256 digest` and `Payload SHA256 digest` for
both), so every release in the repository, older ones included, is signed. Each signature
is made at its package's build time (or at the key's creation, if that is later), so a
package signed again on the next deployment is the same file, and one that has a good
signature by the key already is left as it is: what a dnf or zypper cache holds of the
repository keeps matching the files. The `.rpm`
assets on GitHub Releases stay the unsigned build that `SHA256SUMS` and the build provenance
are about (`gh attestation verify ser2netweb-X.Y.Z-1.x86_64.rpm -R haliskilic/ser2netweb`);
the repository's copy differs from them by its signature header only, so check that one with
`rpm -K`. APT works the same way as the metadata: `InRelease` is signed and holds the
SHA-256 of `Packages`, which holds each package's (`.deb` files carry no signature of their
own).

A `ser2netweb.repo` fetched before the packages were signed says `gpgcheck=0`: it keeps
working, with the signed metadata checked as before. Fetch it again (the install command
above) to have each package checked as well; for zypper, remove the repository and add it
again (`zypper removerepo ser2netweb`, then the commands above), as `addrepo` reads the file
once.

The first deployment with signed packages changes each `.rpm` once (its signature header):
a dnf or zypper that cached the repository's metadata before it says the package doesn't
match what it downloaded until the metadata is read again: `sudo dnf clean metadata` (or
`dnf … --refresh`), `sudo zypper refresh`.

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
3. **sign**: every `.rpm` (`rpmsign --addsign`, after which `rpm/<arch>/repodata/` is made
   again for the signed files), then `InRelease`, `Release.gpg`, `repomd.xml.asc` (SHA-512),
   with the key in the repository secrets `PACKAGES_GPG_PRIVATE_KEY` /
   `PACKAGES_GPG_PASSPHRASE` (only that step gets them; their fingerprint must be the one
   above, the committed `packaging/ser2netweb-archive-keyring.asc`'s and
   `PACKAGES_GPG_FINGERPRINT`'s), in a temporary GnuPG home deleted afterwards (rpmsign's
   gpg reads the passphrase from a file there, never from its arguments); the published
   keyring is the committed file;
4. **verify**: every index against the files it lists, every signature with `gpgv`, every
   `.rpm` with `rpmkeys -K` (a good signature by the site's key, not the digests alone);
5. **test**: `tests/package_repo_smoke.sh` serves the tree on 127.0.0.1 and, in clean
   Debian 12, Ubuntu 24.04, Fedora, AlmaLinux 9 and openSUSE Leap 15.6 containers (x64;
   Debian and Fedora on arm64 too), runs the commands above (`tools/package_repo.py
   commands`): a copy whose `InRelease` and `repomd.xml` were changed after signing must be
   refused, then ser2netweb is installed (dnf and zypper with `gpgcheck=1`),
   `ser2netweb --version` checked, the installed package's signature by the site's key,
   updated and removed;
6. **deploy** to GitHub Pages (the environment `github-pages`, which allows `main` and the
   `v*` tags), then the same commands against the published site.

It runs when release.yml has published a release (its job *start the release channels*
starts it on `main`: GitHub starts no workflow for a release its own token created), for a
release published by hand, and from *Actions → Package repositories → Run workflow*, whose
`tag` builds the repositories as of that release (it and the two before it; a rollback).
Run it **from `main`** (the default) **with `tag`**, for a rollback or a redeployment too,
never from an old tag's or branch's copy of the workflow: that runs the old
`tools/package_repo.py`, and one from before the packages were signed deploys unsigned
`.rpm` files, which a `gpgcheck=1` system refuses.
A pull request that changes the workflow, `packaging/`, the tool or the smoke test builds
and tests a tree signed with a throwaway key made in the job (the `.rpm` files too, checked
with `rpmkeys -K` against that key and installed with `gpgcheck=1`) and deploys nothing; so
do a run started by hand on another branch than `main` and a run without
`PACKAGES_GPG_PRIVATE_KEY` (with a notice): only a release's run and one on `main` or a `v*`
tag get the signing key's secrets and sign with it. Every run replaces the whole
site, so anything else on GitHub Pages would have to be built here too.

Each release adds about 95 MB (four packages); three releases stay well inside GitHub
Pages' 1 GB site limit (`KEEP` in the tool). Locally, with apt-ftparchive, createrepo_c and
rpm (rpmsign, rpmkeys) on `PATH`: `python3 tools/package_repo.py fetch dl`, `build dl site`,
`sign site --test-key`, `verify site --any-key`; the smoke test needs Docker.

Not here (yet): the distributions' own repositories (Debian, Ubuntu, Fedora: a long review
with sponsors and source builds) and Flatpak (a desktop sandbox, not for a system service).
