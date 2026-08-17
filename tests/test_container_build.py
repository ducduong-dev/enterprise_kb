"""The container definitions agree with the code they package.

None of this builds an image — it checks the handful of facts that, when they drift, fail in a
way nobody sees until a deploy: a workspace member missing from the dependency layer, a
service that cannot reach the model proxy because `.env`'s host-side URL leaked into a
container, a published port nothing is listening on.

Each check exists because that exact drift happened.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
SERVICE_DOCKERFILE = (ROOT / "ops" / "docker" / "Dockerfile.service").read_text(encoding="utf-8")
PORTAL_DOCKERFILE = (ROOT / "frontend" / "portal" / "Dockerfile").read_text(encoding="utf-8")

#: Services built from the shared Python image, i.e. everything that speaks to a model or the
#: database. The gateway and the model servers run vendor images.
PLATFORM_SERVICES = [
    name
    for name, service in COMPOSE["services"].items()
    if isinstance(service.get("build"), dict)
    and service["build"].get("dockerfile") == "ops/docker/Dockerfile.service"
]


# ----------------------------------------------------------------- bringing the stack up
#
# Everything here failed a real `make dev-all` on a clean machine.


def test_the_database_image_is_pinned() -> None:
    """`paradedb/paradedb:latest` moved to Postgres 18, which stores its data one directory
    up and refuses to start against this volume — a `docker pull` away from a dev machine
    with no database. The engine version is a decision (ADR-0021), not a moving target."""
    image = str(COMPOSE["services"]["postgres"]["image"])
    assert image.endswith("-pg16"), f"postgres runs {image}; the schema is measured on pg16"
    assert ":latest" not in image


def test_the_bucket_setup_asks_mc_one_target_at_a_time() -> None:
    """`mc anonymous set none a b` is a usage error, so the buckets were never made explicitly
    private and minio-init exited 1 on every start."""
    entrypoint = str(COMPOSE["services"]["minio-init"]["entrypoint"])
    for command in re.findall(r"mc anonymous set \S+ (.+?)(?: &&|\s*\"|$)", entrypoint):
        assert len(command.split()) == 1, f"mc anonymous set takes one target: {command!r}"


def test_the_issuer_is_the_browsers_origin_and_the_endpoints_are_internal() -> None:
    """The login button appears dead when these collapse into one address: the browser cannot
    resolve `keycloak`, and a service cannot fetch keys from its own `localhost`."""
    base = COMPOSE["x-service-base"]["environment"]
    assert base["KB_OIDC_ISSUER"].startswith("${KB_PUBLIC_ORIGIN"), (
        "the issuer must be the browser's origin, since Keycloak stamps it into iss"
    )
    for key in ("KB_OIDC_JWKS_URL", "KB_OIDC_TOKEN_ENDPOINT"):
        assert base[key].startswith("http://keycloak:8080/"), f"{key} must be reachable in-network"

    keycloak = COMPOSE["services"]["keycloak"]["environment"]
    assert keycloak["KC_HOSTNAME"].startswith("${KB_PUBLIC_ORIGIN")
    assert keycloak["KC_PROXY_HEADERS"] == "xforwarded", "behind the portal's nginx"


def test_the_browser_needs_one_port_and_one_origin() -> None:
    """The stack is developed through a forwarded port as often as on the host. A bundle that
    names `localhost:8008` fails there with "Failed to fetch" and nothing else to go on."""
    args = COMPOSE["services"]["portal"]["build"]["args"]
    assert args["VITE_API_BASE"].startswith("${VITE_API_BASE:-/"), args["VITE_API_BASE"]
    assert args["VITE_OIDC_ISSUER"].startswith("${VITE_OIDC_ISSUER:-/"), args["VITE_OIDC_ISSUER"]

    nginx = (ROOT / "frontend" / "portal" / "nginx.conf").read_text(encoding="utf-8")
    assert "proxy_pass http://portal-api:8000/" in nginx
    assert "proxy_pass http://keycloak:8080" in nginx
    assert "realms|resources|js" in nginx, "the login page needs its own assets too"


def test_the_portal_origin_routes_chat_to_chat_api() -> None:
    """The chat page 404'd for a while: the bundle calls `/api/v1/chat/internal`, `/api/`
    forwards to portal-api, and portal-api has no chat route — that endpoint only exists on
    chat-api. The dev server must split `/api` the same way, or `npm run dev` and the image
    disagree about which routes exist."""
    nginx = (ROOT / "frontend" / "portal" / "nginx.conf").read_text(encoding="utf-8")
    assert "location /api/v1/chat/" in nginx, "a longer prefix than /api/, so nginx prefers it"
    assert "proxy_pass http://chat-api:8000/v1/chat/;" in nginx

    vite = (ROOT / "frontend" / "portal" / "vite.config.ts").read_text(encoding="utf-8")
    chat_port = COMPOSE["services"]["chat-api"]["ports"][0].split(":")[0]
    assert f'"/api/v1/chat": {{\n        target: "http://localhost:{chat_port}"' in vite

    api = (ROOT / "frontend" / "portal" / "src" / "api.ts").read_text(encoding="utf-8")
    assert '"/v1/chat/internal"' in api, "the path both proxies are cut for"


def test_only_the_service_the_browser_calls_allows_a_browser_origin() -> None:
    """portal-api is the one the portal's JavaScript talks to. Everything else is called
    service-to-service and should answer no preflight at all."""
    portal_origin = COMPOSE["services"]["portal"]["ports"][0].split(":")[0]
    allowed = COMPOSE["services"]["portal-api"]["environment"]["KB_CORS_ORIGINS"]
    assert f"http://localhost:{portal_origin}" in allowed

    for name in PLATFORM_SERVICES:
        if name == "portal-api":
            continue
        assert "KB_CORS_ORIGINS" not in _environment(name), f"{name} need not answer a browser"


def test_a_service_with_no_http_surface_has_no_http_healthcheck() -> None:
    """The image's healthcheck curls /healthz. A worker never answers it, so the container
    reports unhealthy for its whole life and the column stops meaning anything."""
    for name in ("workflows", "indexer-consumer"):
        check = COMPOSE["services"][name].get("healthcheck")
        assert check == {"disable": True}, f"{name} would run the image's HTTP healthcheck"


def test_the_realm_import_has_no_fields_keycloak_rejects() -> None:
    """One `_comment` key anywhere fails the import, Keycloak exits, and every service comes
    up with nothing to verify tokens against. Notes live in ops/keycloak-realm/README.md."""
    realm = json.loads((ROOT / "ops" / "keycloak-realm" / "kb-realm.json").read_text("utf-8"))

    def keys(node: object) -> list[str]:
        if isinstance(node, dict):
            return [*node.keys(), *(k for value in node.values() for k in keys(value))]
        if isinstance(node, list):
            return [k for item in node for k in keys(item)]
        return []

    assert not [key for key in keys(realm) if key.startswith("_")]


# --------------------------------------------------------------------------- the image


def test_every_workspace_member_is_in_the_dependency_layer() -> None:
    """A missing manifest silently changes what the cached layer resolves."""
    members = sorted(
        path.parent.relative_to(ROOT).as_posix()
        for path in (*ROOT.glob("libs/*/pyproject.toml"), *ROOT.glob("services/*/pyproject.toml"))
    )
    missing = [
        member for member in members if f"COPY {member}/pyproject.toml" not in SERVICE_DOCKERFILE
    ]
    assert not missing, f"Dockerfile.service does not copy: {missing}"


def test_the_image_installs_what_opencv_and_paddle_need() -> None:
    """A scanned document is the first thing that finds these missing, in production."""
    assert "libgl1" in SERVICE_DOCKERFILE
    assert "libglib2.0-0" in SERVICE_DOCKERFILE


def test_the_image_runs_as_a_non_root_user() -> None:
    assert re.search(r"^USER kb$", SERVICE_DOCKERFILE, re.M)


def test_the_healthcheck_uses_a_tool_the_image_has() -> None:
    assert "curl" in SERVICE_DOCKERFILE and "apt-get install" in SERVICE_DOCKERFILE


# ------------------------------------------------------------------------ model wiring


@pytest.mark.parametrize("service", PLATFORM_SERVICES)
def test_every_service_reaches_the_proxy_by_service_name(service: str) -> None:
    """`.env` is written for tooling on the host, where the proxy is `localhost:4000`. Inside
    a container that is the container itself, and the model call fails on the first document
    rather than at start-up (ADR-0024)."""
    environment = _environment(service)
    assert environment.get("KB_MODEL_PROXY_URL", "").startswith("http://litellm:"), (
        f"{service} would resolve the model proxy to {environment.get('KB_MODEL_PROXY_URL')!r}"
    )


#: retrieval-api *is* the funnel; it has no funnel to call. Everything else that shows a user
#: document text goes through one (INV-1).
FUNNEL_SERVICES = {"retrieval-api", "dmz-retrieval-api"}


@pytest.mark.parametrize(
    "service", [name for name in PLATFORM_SERVICES if name not in FUNNEL_SERVICES]
)
def test_every_service_reaches_the_funnel_by_service_name(service: str) -> None:
    """Same trap, one setting along: retrieval is the only way to read a document (INV-1)."""
    url = _environment(service).get("KB_RETRIEVAL_URL", "")
    assert url.startswith("http://") and "localhost" not in url, (
        f"{service} would resolve the retrieval funnel to {url!r}"
    )


def test_the_dmz_can_reach_the_proxy_and_nothing_else_new() -> None:
    """A public answer needs a model. It still must not reach the portal or Temporal."""
    litellm_networks = set(COMPOSE["services"]["litellm"]["networks"])
    for service in ("dmz-chat-api", "dmz-retrieval-api"):
        networks = set(COMPOSE["services"][service]["networks"])
        assert "dmz-models" in networks, f"{service} cannot reach the model proxy"
        assert networks <= {"dmz", "dmz-data", "dmz-models"}, (
            f"{service} joins a network beyond the DMZ's three: {networks}"
        )
    assert "dmz-models" in litellm_networks


def test_the_dmz_services_are_bound_to_the_public_surface() -> None:
    for service in ("dmz-chat-api", "dmz-retrieval-api"):
        assert _environment(service)["KB_SURFACE"] == "dmz"


# ----------------------------------------------------------------------------- the portal


def test_the_portal_publishes_the_port_nginx_listens_on() -> None:
    """It served nothing for a while: nginx listens on 80 and compose published 5173:5173."""
    published = COMPOSE["services"]["portal"]["ports"]
    assert published == ["5173:80"]
    assert "EXPOSE 80" in PORTAL_DOCKERFILE


def test_the_portal_takes_its_configuration_at_build_time() -> None:
    """Vite inlines `VITE_*` into the bundle; setting them at runtime ships the defaults."""
    build = COMPOSE["services"]["portal"]["build"]
    assert "VITE_API_BASE" in build["args"]
    assert "ARG VITE_API_BASE" in PORTAL_DOCKERFILE
    assert "environment" not in COMPOSE["services"]["portal"]


def test_the_portal_serves_a_single_page_app() -> None:
    """Without the fallback, refreshing on /review/<id> is a 404 the router never sees."""
    nginx = (ROOT / "frontend" / "portal" / "nginx.conf").read_text(encoding="utf-8")
    assert "try_files $uri $uri/ /index.html" in nginx


# ------------------------------------------------------------------------------- the proxy


def test_the_proxy_healthcheck_uses_a_tool_its_image_has() -> None:
    """`ghcr.io/berriai/litellm` has python and no curl."""
    check = COMPOSE["services"]["litellm"]["healthcheck"]["test"]
    assert "python" in check
    assert not any("curl" in str(part) for part in check)


def test_the_proxy_config_lists_every_role_the_platform_asks_for() -> None:
    config = yaml.safe_load((ROOT / "ops" / "litellm" / "config.yaml").read_text(encoding="utf-8"))
    aliases = {model["model_name"] for model in config["model_list"]}
    assert {"kb-generation", "kb-vlm", "kb-embedding", "kb-rerank"} <= aliases


def test_the_proxy_does_not_log_the_documents_that_pass_through_it() -> None:
    """The platform's audit trail is where answers are recorded (INV-11); a copy inside a
    proxy's logs is a second place to leak from."""
    config = yaml.safe_load((ROOT / "ops" / "litellm" / "config.yaml").read_text(encoding="utf-8"))
    assert config["litellm_settings"]["turn_off_message_logging"] is True


def test_no_public_provider_is_routed_by_default() -> None:
    """A route out of the bank is a decision with a name on it ([OPEN]-1, ADR-0025)."""
    config = yaml.safe_load((ROOT / "ops" / "litellm" / "config.yaml").read_text(encoding="utf-8"))
    for model in config["model_list"]:
        target = str(model["litellm_params"]["model"])
        assert "api_base" in model["litellm_params"], (
            f"{model['model_name']} routes to {target} with no on-premises api_base"
        )


# --------------------------------------------------------------------------------- helpers


def _environment(service: str) -> dict[str, str]:
    """A service's compose-level environment, merged over the base anchor."""
    raw = COMPOSE["services"][service].get("environment") or {}
    if isinstance(raw, list):  # pragma: no cover - the file uses mapping form
        return dict(item.split("=", 1) for item in raw)
    return {key: str(value) for key, value in raw.items()}


def test_the_proxy_config_is_mounted_as_a_directory() -> None:
    """A single-file bind mount pins an inode, and every editor that saves by write-then-rename
    gives the file a new one — so the container serves the config from before the edit and a
    restart does not fix it, because the mount still points at the old inode."""
    mounts = COMPOSE["services"]["litellm"]["volumes"]
    assert any(str(m).startswith("./ops/litellm:") for m in mounts), mounts
    assert not any("config.yaml:" in str(m) for m in mounts), (
        "mount the directory; a file mount goes stale on the first edit"
    )


def test_no_route_falls_back_to_itself() -> None:
    """A fallback to the same alias is not a safety net: it retries the same dead route and
    doubles the error. And the rule that matters when a second route is added — never fall back
    from an on-premises model to a public one, which turns an outage into an unreviewed
    disclosure ([OPEN]-1)."""
    config = yaml.safe_load((ROOT / "ops" / "litellm" / "config.yaml").read_text(encoding="utf-8"))
    for entry in config["litellm_settings"].get("fallbacks") or []:
        for alias, targets in entry.items():
            assert alias not in targets, f"{alias} falls back to itself"


def test_the_reasoning_model_has_thinking_turned_off() -> None:
    """Qwen3.5 bills its reasoning trace against the same `max_tokens` as the answer, so a
    budget exhausted mid-trace truncates the answer or empties it. Measured through the proxy:
    800 tokens and a cut-off answer with thinking on, 52 and a complete one without.

    Asserted on `extra_body` specifically. `allowed_openai_params: ["chat_template_kwargs"]`
    looks equivalent and breaks every request — LiteLLM forwards the key as a keyword argument
    to the OpenAI SDK client, which rejects it as unexpected.
    """
    config = yaml.safe_load((ROOT / "ops" / "litellm" / "config.yaml").read_text(encoding="utf-8"))
    generation = next(m for m in config["model_list"] if m["model_name"] == "kb-generation")
    params = generation["litellm_params"]
    assert "allowed_openai_params" not in params, (
        "this breaks every request to the alias; use extra_body"
    )
    assert params["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False
