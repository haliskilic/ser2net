# Code-signing the Windows executable

An unsigned `ser2net-windows-x64.exe` makes Windows SmartScreen warn on the first run
("Windows protected your PC") and some antivirus products flag it more readily. The
release workflow signs the exe **when a signing service is configured**; without one,
releases stay unsigned exactly as before — they can still be verified with `SHA256SUMS`
and the build provenance (see below).

Signing happens only for release tags (not for pull requests), in
`.github/workflows/standalone.yml`, before the exe is tested and published, and the
workflow then checks that the signature is valid. Code-signing keys can no longer be
kept in a plain `.pfx` file (CA/Browser Forum rules since 2023 require a hardware or
cloud key store), so both supported options are cloud signing services. Check each
provider's eligibility and pricing yourself — they decide who qualifies.

Pick one and set **Settings → Secrets and variables → Actions** of the repository:

## Option A — SignPath (SignPath Foundation offers free signing to open-source projects)

1. Apply for the open-source program at <https://signpath.org> (the project must be
   open source and built by CI from its public repository) or use a SignPath
   subscription.
2. In SignPath: a project (e.g. `ser2net`), an artifact configuration for a single
   PE file, and a signing policy (e.g. `release-signing`); connect the GitHub
   repository as a trusted build system and create an API token for CI.
3. Repository **variables**: `WINDOWS_SIGNING` = `signpath`,
   `SIGNPATH_ORGANIZATION_ID`, `SIGNPATH_PROJECT_SLUG`, `SIGNPATH_POLICY_SLUG`.
   **Secret**: `SIGNPATH_API_TOKEN`.

The workflow uploads the unsigned exe as an artifact, submits it to SignPath, waits for
the signed file and publishes that.

## Option B — Azure Trusted Signing (Microsoft, monthly fee)

1. Create a Trusted Signing account and a certificate profile (public trust) in Azure;
   the identity validation there decides who is eligible.
2. Register an app (service principal) and give it the *Trusted Signing Certificate
   Profile Signer* role on the account.
3. Repository **variables**: `WINDOWS_SIGNING` = `azure`, `AZURE_SIGNING_ENDPOINT`
   (e.g. `https://weu.codesigning.azure.net/`), `AZURE_SIGNING_ACCOUNT`,
   `AZURE_SIGNING_PROFILE`. **Secrets**: `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`,
   `AZURE_CLIENT_SECRET`.

## Checking a release

- Signature (Windows PowerShell): `Get-AuthenticodeSignature .\ser2net-windows-x64.exe`
- Checksums: `sha256sum -c SHA256SUMS --ignore-missing` (Linux) or
  `Get-FileHash .\ser2net-windows-x64.exe` (compare with `SHA256SUMS`)
- Build provenance (any file of the release, needs the GitHub CLI):
  `gh attestation verify ser2net-windows-x64.exe -R haliskilic/ser2net`
