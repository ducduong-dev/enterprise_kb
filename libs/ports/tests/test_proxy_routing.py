"""Where a model call goes, and whether it may go there (ADR-0024, [OPEN]-1)."""

from __future__ import annotations

import pytest
from kb_common.config import ModelGatewaySettings
from kb_common.errors import ConfigError
from kb_ports.proxy import route
from pydantic import SecretStr


def settings(**overrides: object) -> ModelGatewaySettings:
    base: dict[str, object] = {
        "use_proxy": True,
        "proxy_url": "http://litellm:4000",
        "proxy_api_key": SecretStr("sk-test"),
        "generation_model": "kb-generation",
        "vlm_model": "kb-vlm",
        "embedding_model": "kb-embedding",
        "rerank_model": "kb-rerank",
    }
    base.update(overrides)
    return ModelGatewaySettings(**base)  # type: ignore[arg-type]


def test_every_role_resolves_to_the_one_proxy() -> None:
    """Four roles, one endpoint and one credential — that is the point of the proxy."""
    cfg = settings()
    routes = [route(role, cfg) for role in ("generation", "vlm", "embedding", "rerank")]

    assert {resolved.base_url for resolved in routes} == {"http://litellm:4000"}
    assert all(resolved.proxied for resolved in routes)
    assert all(resolved.headers["Authorization"] == "Bearer sk-test" for resolved in routes)
    assert [resolved.model for resolved in routes] == [
        "kb-generation",
        "kb-vlm",
        "kb-embedding",
        "kb-rerank",
    ]


def test_without_the_proxy_each_role_keeps_its_own_server() -> None:
    """A deployment that has not adopted the proxy behaves exactly as it did before."""
    cfg = settings(use_proxy=False, vlm_url="http://gpu-node:8083")
    assert route("vlm", cfg).base_url == "http://gpu-node:8083"
    assert route("vlm", cfg).proxied is False


def test_no_direct_default_points_a_container_at_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    """These defaults read `http://localhost:<published port>` for a long time, which inside a
    service container is that container. `use_proxy=False` then failed with a connection refused
    naming the caller, not the model server — and only on the fallback path, so nothing noticed
    while the proxy was up. Service name and *container* port, as `proxy_url` already had it."""
    roles = ("generation", "vlm", "embedding", "rerank")
    # The default is the subject here, so the ambient `.env` must not answer for it.
    for role in roles:
        monkeypatch.delenv(f"KB_MODEL_{role.upper()}_URL", raising=False)

    defaults = ModelGatewaySettings()
    urls = {role: getattr(defaults, f"{role}_url") for role in roles}

    for role, url in urls.items():
        assert "localhost" not in url, f"{role} would resolve to the calling container"
        assert "127.0.0.1" not in url, f"{role} would resolve to the calling container"

    # `test_the_direct_defaults_name_services_that_exist` checks these against compose.
    for role, url in urls.items():
        assert route(role, settings(use_proxy=False, **{f"{role}_url": url})).base_url == url


def test_a_route_that_leaves_the_network_is_refused_by_default() -> None:
    """[OPEN]-1 is a Compliance decision, so the platform will not make it by accident."""
    cfg = settings(vlm_model="gemini-2.0-flash", external_models=frozenset({"gemini-2.0-flash"}))
    with pytest.raises(ConfigError) as exc:
        route("vlm", cfg)
    assert exc.value.detail["open_item"] == "[OPEN]-1"
    assert "KB_MODEL_ALLOW_EXTERNAL_PROCESSING" in exc.value.detail["remedy"]


def test_an_external_route_is_allowed_once_compliance_has_ruled() -> None:
    cfg = settings(
        vlm_model="gemini-2.0-flash",
        external_models=frozenset({"gemini-2.0-flash"}),
        allow_external_processing=True,
    )
    resolved = route("vlm", cfg)
    assert resolved.leaves_network is True
    assert resolved.describe()["model"] == "gemini-2.0-flash"


def test_the_ruling_applies_per_model_not_per_deployment() -> None:
    """Transcribing scans on a public vision model does not make the answer model public."""
    cfg = settings(
        vlm_model="gemini-2.0-flash",
        external_models=frozenset({"gemini-2.0-flash"}),
        allow_external_processing=True,
    )
    assert route("vlm", cfg).leaves_network is True
    assert route("generation", cfg).leaves_network is False


def test_the_pre_proxy_hosted_generation_flag_still_means_hosted() -> None:
    """`generation_backend=api` predates the proxy and said the same thing; it keeps saying it."""
    cfg = settings(use_proxy=False, generation_backend="api")
    with pytest.raises(ConfigError):
        route("generation", cfg)

    allowed = settings(use_proxy=False, generation_backend="api", allow_external_processing=True)
    assert route("generation", allowed).leaves_network is True
