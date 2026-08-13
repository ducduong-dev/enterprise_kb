# The dev realm

`kb-realm.json` is imported by the `keycloak` container at start-up (`start-dev
--import-realm`). Production brokers AD/LDAP into these same groups and roles; the users and
credentials here exist only so the tests and the ACL sweep run against the identities in
`libs/authz/src/kb_authz/fixtures.py`. Every dev password is `dev`.

## One issuer, two addresses

`KC_HOSTNAME` pins the issuer to `KB_PUBLIC_ORIGIN` (default `http://localhost:5173`) — the
portal's own origin, which proxies `/realms` through to Keycloak. That is the URL the browser
signs in at and the `iss` every service verifies against. Inside compose that host is the
*container itself*, so the services reach the same Keycloak by service name for the two things
they do server-side: `KB_OIDC_JWKS_URL` (fetch the signing keys) and `KB_OIDC_TOKEN_ENDPOINT`
(RFC 8693 exchange, INV-3).

Collapsing the two is the failure this note exists for. Point everything at `keycloak:8080`
and the browser cannot resolve it — the portal's Đăng nhập button fetches the provider
metadata, the fetch fails, and nothing visibly happens. Point everything at `localhost:8080`
and the services cannot fetch the keys, so every token is rejected as unverifiable. The issuer
is an identifier; the endpoints are network locations, and in a segmented network they differ
in production too.

**Keycloak rejects unknown fields in a realm import.** This file therefore carries no
`_comment` keys — a single one anywhere in it fails the import, Keycloak exits, and every
service that verifies a token comes up with nothing to verify against. Notes about the realm
belong in this file instead.

## What the shape of it encodes

* `/restricted/board` is **deliberately empty**. The canary documents are ACL'd to that group,
  so any principal that retrieves one has escaped the filter (INV-2). The ACL sweep asserts
  exactly that; a member added here would silently disarm it.
* `kb-portal` is the only public client, and the only one with direct access grants — the
  browser gets a user token, and every service-to-service call is a token exchange from it
  (INV-3, RFC 8693).
* Realm roles are the ones `kb_authz.principal.Role` knows: `kb-steward`, `kb-approver`,
  `kb-archive-reader`, `kb-pii-overrider`, `kb-legal`, `kb-admin`. A role added here that the
  code does not know does nothing; a role the code checks and the realm lacks locks the
  feature out entirely.

## Re-importing after a change

The import runs once against an empty Keycloak database. After editing this file:

```bash
docker compose rm -sf keycloak && docker volume rm kb-platform_postgres-data   # dev only
make dev
```

Or delete the realm in the admin console (`http://localhost:8080`, `admin`/`admin`) and
restart the container.
