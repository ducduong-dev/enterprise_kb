# ADR-0020 — The bot borrows a user's visibility, never its own

**Status:** accepted · **Date:** 2026-08-11

## Context

The internal chatbot is called by connectors — a Teams app, an intranet widget — each with its
own service account. Those accounts need to authenticate to chat-api, but they must grant no
document visibility at all (INV-3): a bot that could read everything would answer everything,
to whoever asked it.

Three ways to bridge that gap were available:

1. **The connector forwards the end user's access token.** Simple, and it means every connector
   holds a credential it can replay against any service that accepts it, for as long as the
   token lives.
2. **chat-api trusts a header naming the user** and builds a filter from it. This makes the
   header an authorization decision, and headers are attacker-controlled.
3. **RFC 8693 token exchange.**

## Decision

A service account calling `/v1/chat/{surface}` must name the person it is acting for
(`X-KB-On-Behalf-Of`), and chat-api exchanges its token for one issued for that user before
anything is retrieved. The exchanged token's `sub` is the user and its `act.sub` is the bot,
which is what `kb_authz` turns into `Principal(kind=internal_bot, on_behalf_of=user)`.

The header is not the authorization. Keycloak's refusal is: if the bot is not permitted to
impersonate that user, the exchange fails and so does the request. There is no fallback to
answering as the bot — the bot can see nothing, and "no results" is indistinguishable to a user
from "nothing exists", so a fallback would turn an authorization failure into a silent lie.

An exchanged token whose claims record no delegation is refused too. Without the `act` claim
nothing names the bot, and the user's reads would be attributed to nobody.

## Consequences

* Every answer's audit record names both parties, and the ACL sweep can assert that a bot
  acting for retail staff sees exactly what retail staff sees — no more (`a051`–`a054` in the
  answer set).
* Connectors never hold a user's access token; the exchanged token is scoped to the delegation
  and short-lived, cached per user until shortly before it expires.
* A Keycloak outage stops the internal bot. That is the correct failure direction: the
  alternative is answering without knowing on whose authority.
