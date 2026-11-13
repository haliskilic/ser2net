# Sign-in with LDAP / Active Directory or OpenID Connect

Besides its own accounts, ser2netweb can let users sign in against a directory (**Settings →
LDAP / Active Directory**, needs the optional `ldap3` package) or an identity provider
(**Settings → Single sign-on (OIDC)**, needs `authlib`). Both are in the standalone
executables and the Docker image. The user's groups decide the role (admin, operator,
viewer); local accounts keep working beside them, and two-factor sign-in applies to local
accounts only.

Each directory or SSO user gets a *shadow account* of that name (no password, its role
refreshed at every sign-in), which is how the Users panel shows them. A sign-in never takes
over another kind of account: when a local account (or an account of the other method) has
the name, letter case aside, the sign-in is refused and logged. Keep a local admin account
in case the directory is unreachable; `ser2netweb --set-password NAME` makes one offline.

## Groups and roles

**Admin group**, **Operator group** and **Viewer group** each name one group; the highest
one a user is in gives the role, else **Default role** (*— deny login —*: refused). A group
setting matches only a whole group name, in any letter case (Turkish İ/ı too):

- a group's own name: the CN of a DN (`ser2netweb-admins` matches
  `CN=ser2netweb-admins,OU=Groups,DC=example,DC=com`), the last part of a path (Keycloak's
  `/team/ser2netweb-admins`) or the part after `DOMAIN\`;
- the full DN, or its leading RDNs (`cn=ser2netweb-admins,ou=groups`);
- the value exactly as the provider sends it (an Entra ID group's Object ID).

Part of a name doesn't match (`admins` doesn't match `ser2netweb-admins` or `Domain Admins`).
2.8.0 and older matched any text inside a group name: a setting that only matched that way
is logged as a `WARNING` at the user's sign-in and shown in Settings, with the value to
enter instead.

## LDAP / Active Directory

| Field | Active Directory | OpenLDAP and others |
|---|---|---|
| **Server URI** | `ldaps://dc1.example.com:636` | `ldaps://ldap.example.com` |
| **User DN template** (direct bind) | `{username}@example.com` or `EXAMPLE\{username}` | `uid={username},ou=people,dc=example,dc=com` |
| or **Bind DN** + **Bind password**, **User search base**, **User search filter** (search + bind) | `cn=svc-ser2net,ou=service,dc=example,dc=com`, `dc=example,dc=com`, `(sAMAccountName={username})` | `cn=reader,dc=example,dc=com`, `ou=people,dc=example,dc=com`, `(uid={username})` |
| **Group attribute** | `memberOf` | `memberOf` (the `memberof` overlay) |
| **Also count nested groups** | groups through other groups (`LDAP_MATCHING_RULE_IN_CHAIN`) | — |

- **Direct bind** binds with the user's own name and password. With an AD sign-in name
  (`user@domain`, `DOMAIN\user`) ser2netweb then looks the user up (by `userPrincipalName`,
  or `sAMAccountName` in that domain) to read the groups, under the user search base if
  one is set, else the domain the server names.
- **Search + bind** binds as the service account, finds the user with the filter, then
  checks the password with a second bind as the user's DN.
- **TLS:** `ldaps://` or **StartTLS** checks the server's certificate and host name against
  the CAs the system trusts, or only the ones in **CA certificate file** (PEM, or DER as
  Windows exports it; enter its full path, in Docker for example `/data/corp-ca.pem`).
  **Don't check the server certificate** turns that off (a warning stays on the page).
  Plain `ldap://` without StartTLS sends passwords unencrypted (warned too).
- A failed bind, a user not found, a refused certificate and which groups a user had when
  none matched are written to `all.log` (`LDAP login denied for 'name': no group setting
  matches their groups: …`); the password is never logged.

## OpenID Connect

1. Register ser2netweb as a confidential web client (authorization code flow) at the
   provider. The **Redirect URI to register** is shown at the top of the OIDC panel:
   `https://ser2netweb.example.com/auth/oidc/callback`. Behind a reverse proxy set it in
   **Redirect URI**, or set **Settings → Reverse proxy → Public URL** (the redirect URI is
   then made from it); most providers accept `http://` only for `localhost`.
2. Enter **Issuer URL**, **Client ID** and **Client secret**. Every address the provider's
   discovery document names must be `https://` (or `http://` to this machine's loopback).
3. Choose the **Username claim** (default `preferred_username`; else `email`, then `sub`
   are tried; an e-mail address the provider marks `email_verified: false` names nobody)
   and the **Groups/roles claim** (default `groups`).

ser2netweb reads only the **ID token** (signature, issuer, audience, expiry and nonce checked,
two minutes of clock skew allowed); it doesn't call the userinfo endpoint. So the groups
must be a **top-level claim of the ID token**, a list of strings (or one string).

| Provider | What to set |
|---|---|
| **Keycloak** | Issuer `https://keycloak.example.com/realms/main`. Add a *Group Membership* mapper (claim `groups`, *Add to ID token* on; *Full group path* gives `/team/ser2netweb-admins`, which the group's own name matches). Realm and client roles are nested (`realm_access.roles`) and can't be read: map them with a *User Realm Role* mapper to a flat claim such as `roles` (*Add to ID token* on) and set that as **Groups/roles claim**. |
| **Entra ID** (Azure AD) | Issuer `https://login.microsoftonline.com/<tenant id>/v2.0`. The `groups` claim (Token configuration → *Add groups claim*) carries the groups' **Object IDs**: enter the IDs as the group settings. A user in more than 200 groups gets no `groups` claim at all: use **app roles** instead (App roles, assigned to users or groups) with **Groups/roles claim** `roles`, and the role values as the group settings. |
| **Google** | Issuer `https://accounts.google.com`. Google sends no groups, so only **Default role** can give a role, and there is no allowed-domain setting: with an *External* OAuth consent screen any Google account gets it. Use an *Internal* consent screen (Google Workspace), or a provider in between that adds groups (Keycloak, Authentik, Dex). |
| **Authentik, Okta, Dex, others** | The issuer URL from the provider; a groups claim in the ID token (Okta: a *Groups claim* filter on the app's ID token). |

The sign-in sends PKCE (S256), and the code, the client secret and the keys travel only
over `https`. A user whose groups give no role sees *Your account isn't mapped to a role*;
`all.log` says which groups the ID token had (`OIDC login denied for 'name': no group
setting matches their groups: …`). Other lines to look for: `OIDC login init` (the redirect
URI sent), `OIDC: the identity provider refused the sign-in`, `OIDC: the ID token was
refused (…)` with a hint when the clocks differ, `OIDC: nonce mismatch`, `OIDC sign-in
failed: …` (an `http://` address in the discovery document).

## Limits

- One directory and one identity provider; no SAML, no SCIM.
- Roles are refreshed at sign-in only: a user removed from a group keeps the role until the
  session ends (**Settings → Web UI access → Session lifetime**; **Sign out everyone**
  ends all sessions at once).
- A failed directory or SSO sign-in is recorded like a failed password sign-in: in the
  audit log, and with the *Failed sign-in* notification when it is ticked.
