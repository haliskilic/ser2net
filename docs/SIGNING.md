# Code-signing the Windows executable

An unsigned `ser2netweb-windows-x64.exe` makes Windows SmartScreen warn on the first run
("Windows protected your PC") and some antivirus products flag it more readily. The
release workflow signs the exe **when a signing service is configured**; without one,
releases stay unsigned exactly as before — they can still be verified with `SHA256SUMS`
and the build provenance (see below).

Signing happens only for release tags (not for pull requests, `main` or a release dry
run), in `.github/workflows/standalone.yml`, which `release.yml` runs through `ci.yml`,
before the exe is tested and published, and the workflow then checks that the signature
is valid. Code-signing keys can no longer be kept in a plain `.pfx` file (CA/Browser Forum
rules since 2023 require a hardware or cloud key store), so both supported options are
cloud signing services. Check each provider's eligibility and pricing yourself — they
decide who qualifies.

The workflow (`standalone.yml`, at its top level) and the jobs that call it in `ci.yml` and
`release.yml` already grant `actions: read` — the SignPath action hands `GITHUB_TOKEN` to its connector
to download the build artifact, which needs that scope even on a public repo. The exe
carries a version resource and the ser2netweb icon, so Explorer's *Details* tab shows the
product, publisher and version (SmartScreen and UAC still say "Unknown publisher" until it
is signed). You can rehearse the signing *plumbing* (version resource + the
verify/copy steps) without an account by running the workflow by hand: **Actions →
Standalone → Run workflow**, with **sign_dry_run** ticked (it runs only while the
`WINDOWS_SIGNING` variable is empty, before a provider is set up). It signs the exe with a
throwaway self-signed certificate on the runner and checks the signature is valid. It does
not exercise the provider, so it is only a smoke test of the steps, not of your account.

Pick one and set **Settings → Secrets and variables → Actions** of the repository.
`release.yml` hands `ci.yml` these four secrets by name and no others (`AZURE_TENANT_ID`,
`AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, `SIGNPATH_API_TOKEN`), and `ci.yml` passes them on
to the standalone workflow only for a release build. A signing setup that needs another
secret has to be added in all three files: `release.yml`'s `ci` job, `ci.yml`'s
`workflow_call` secrets and `standalone` job, and the `secrets:` that `standalone.yml`
declares.

## Option A — SignPath (SignPath Foundation offers free signing to open-source projects)

1. Apply for the open-source program at <https://signpath.org> (the project must be
   open source and built by CI from its public repository) or use a SignPath
   subscription.
2. In SignPath: a project (e.g. `ser2netweb`), an artifact configuration, and a signing
   policy (e.g. `release-signing`); connect the GitHub repository as a trusted build
   system and create an API token for CI.
   - The workflow uploads the exe with `actions/upload-artifact`, which **zips** it, so
     the artifact configuration must have a `<zip-file>` root containing `ser2netweb.exe`
     (not a bare `<pe-file>`; `ser2net.exe` up to 2.9.x: an existing configuration needs
     the new name), e.g.
     ```xml
     <artifact-configuration xmlns="http://signpath.io/artifact-configuration/v1">
       <zip-file>
         <pe-file path="ser2netweb.exe">
           <authenticode-sign/>
         </pe-file>
       </zip-file>
     </artifact-configuration>
     ```
   - A release policy usually needs a **manual approval** in SignPath before it signs.
     The workflow waits up to 30 min for that (`wait-for-completion-timeout-in-seconds`);
     raise it if your approvers are slower.
3. Repository **variables**: `WINDOWS_SIGNING` = `signpath`,
   `SIGNPATH_ORGANIZATION_ID`, `SIGNPATH_PROJECT_SLUG`, `SIGNPATH_POLICY_SLUG`.
   **Secret**: `SIGNPATH_API_TOKEN`.

The workflow uploads the unsigned exe as an artifact, submits it to SignPath, waits for
the signed file (extracted from the returned zip) and publishes that.

## Option B — Azure Artifact Signing (Microsoft, monthly fee)

Formerly *Trusted Signing*; the workflow uses the renamed `Azure/artifact-signing-action`
(pinned by commit SHA).

1. Create an Artifact Signing account and a certificate profile (public trust) in Azure;
   the identity validation there decides who is eligible.
2. Register an app (service principal) and give it the *Trusted Signing Certificate
   Profile Signer* role on the account.
3. Repository **variables**: `WINDOWS_SIGNING` = `azure`, `AZURE_SIGNING_ENDPOINT`
   (e.g. `https://weu.codesigning.azure.net/`), `AZURE_SIGNING_ACCOUNT`,
   `AZURE_SIGNING_PROFILE`. **Secrets**: `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`,
   `AZURE_CLIENT_SECRET`.

A client secret lives long and works from anywhere until it is rotated. Azure's workload
identity federation (a federated credential that trusts only this repository's release
tags, no secret at all) is the better choice, but it needs `id-token: write` in the job
that signs. `standalone.yml` can't ask for that: CI runs the same workflow for every pull
request, and a reusable workflow that requests more than its caller grants fails before it
starts. Moving the signing step into a job of `release.yml` would allow it; do that before
setting `WINDOWS_SIGNING` = `azure`, or keep the secret short-lived.

## Checking a release

- Signature (Windows PowerShell): `Get-AuthenticodeSignature .\ser2netweb-windows-x64.exe`
- Checksums: `sha256sum -c SHA256SUMS --ignore-missing` (Linux) or
  `Get-FileHash .\ser2netweb-windows-x64.exe` (compare with `SHA256SUMS`)
- Build provenance (the three executables, needs the GitHub CLI):
  `gh attestation verify ser2netweb-windows-x64.exe -R haliskilic/ser2netweb`; add
  `--signer-workflow haliskilic/ser2netweb/.github/workflows/release.yml` to also require
  that the release workflow made it (only its tag runs attest anything)
- The Docker image: `gh attestation verify oci://ghcr.io/haliskilic/ser2net:X.Y.Z -R haliskilic/ser2netweb`.
  `:X.Y` and `:latest` may point at a weekly rebuild of the latest release on a newer base
  image (docs/DOCKER.md), attested by `.github/workflows/image-refresh.yml` instead of
  `release.yml`
