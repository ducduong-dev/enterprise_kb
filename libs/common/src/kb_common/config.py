"""Process configuration.

One settings object per process, loaded from environment (12-factor). Every value that
differs between dev / prod / DMZ must live here, never in code. See ops/ for the
declarative deployment config that supplies these ([OPEN]-5 keeps ops config declarative).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["dev", "test", "staging", "prod"]
Surface = Literal["internal", "dmz"]


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KB_DB_", extra="ignore")

    host: str = "localhost"
    port: int = 5432
    name: str = "kb"
    user: str = "kb"
    password: SecretStr = SecretStr("kb")
    pool_size: int = 10
    max_overflow: int = 5
    statement_timeout_ms: int = 15_000

    @property
    def url(self) -> str:
        return (
            f"postgresql+psycopg://{self.user}:{self.password.get_secret_value()}"
            f"@{self.host}:{self.port}/{self.name}"
        )


class StorageSettings(BaseSettings):
    """MinIO / S3. Keys are content-hash addressed (see StoragePort)."""

    model_config = SettingsConfigDict(env_prefix="KB_S3_", extra="ignore")

    endpoint: str = "http://localhost:9000"
    access_key: SecretStr = SecretStr("minioadmin")
    secret_key: SecretStr = SecretStr("minioadmin")
    bucket_originals: str = "kb-originals"
    bucket_derived: str = "kb-derived"
    region: str = "us-east-1"
    secure: bool = False


class KeywordIndexSettings(BaseSettings):
    """Keyword engine. [OPEN]-2 was resolved in M7 by the bake-off: pg_search (ADR-0021).

    `postgres_fts` remains selectable for a deployment without the ParadeDB extension — it is
    not a BM25 engine and says so in its adapter info, but it keeps the platform runnable on a
    stock Postgres."""

    model_config = SettingsConfigDict(env_prefix="KB_KEYWORD_", extra="ignore")

    backend: Literal["pg_search", "postgres_fts"] = "pg_search"
    url: str = "http://localhost:9200"
    index: str = "kb-chunks"
    username: str | None = None
    password: SecretStr | None = None


class IdentitySettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KB_OIDC_", extra="ignore")

    issuer: str = "http://localhost:8080/realms/kb"
    jwks_url: str | None = None
    audience: str = "kb-platform"
    # Claim names as mapped by the Keycloak realm in ops/keycloak-realm/.
    groups_claim: str = "groups"
    roles_claim: str = "kb_roles"
    department_claim: str = "department"
    # Token exchange (INV-3): the internal bot swaps its token for the end user's.
    token_endpoint: str | None = None
    internal_bot_client_id: str = "kb-internal-bot"
    external_bot_client_id: str = "kb-external-bot"
    leeway_seconds: int = 30
    # dev/test only: accept unsigned tokens from the fixture issuer.
    allow_insecure_tokens: bool = False

    @property
    def resolved_jwks_url(self) -> str:
        return self.jwks_url or f"{self.issuer}/protocol/openid-connect/certs"


class ModelGatewaySettings(BaseSettings):
    """All model endpoints (INV-12). Services never talk to these directly, only ports do.

    Every model call — generation, vision, embedding, rerank — goes through one LiteLLM proxy
    (ADR-0024). The services know *roles* ("the vision model"); the proxy knows providers, keys,
    fallbacks and cost. `use_proxy=False` keeps the direct vLLM/TEI endpoints for a deployment
    that has not adopted it, and the adapters behave identically either way.
    """

    model_config = SettingsConfigDict(env_prefix="KB_MODEL_", extra="ignore")

    # --------------------------------------------------------------------------- the proxy
    use_proxy: bool = True
    proxy_url: str = "http://litellm:4000"
    proxy_api_key: SecretStr | None = None

    #: Model aliases whose route leaves the bank's network. The routing itself lives in
    #: `ops/litellm/config.yaml`; this is the platform's own declaration of it, because an
    #: adapter must be able to record `leaves_network` truthfully without interrogating the
    #: proxy — and because a mistake here is caught by the guard below rather than discovered
    #: in an audit.
    external_models: frozenset[str] = frozenset()
    #: [OPEN]-1. Sending document text or page images to a provider outside the bank is a
    #: Compliance decision, so the platform refuses to do it until this is switched on —
    #: whatever the proxy is configured to route.
    allow_external_processing: bool = False

    # -------------------------------------------------------------------- roles and models
    embedding_url: str = "http://localhost:8081"
    embedding_model: str = "BAAI/bge-m3"
    embedding_dim: int = 1024
    rerank_url: str = "http://localhost:8082"
    rerank_model: str = "BAAI/bge-reranker-v2-m3"
    vlm_url: str = "http://localhost:8083"
    vlm_model: str = "Qwen/Qwen2.5-VL-7B-Instruct"
    #: How scanned pages are read: `paddle` runs OCR and escalates the pages it cannot read to
    #: the vision model; `vlm` transcribes every page with the vision model (ADR-0025).
    ocr_engine: Literal["paddle", "vlm"] = "paddle"
    # [OPEN]-1: local vLLM vs API model, pending the Compliance ruling. Default local.
    generation_backend: Literal["vllm", "api"] = "vllm"
    generation_url: str = "http://localhost:8084"
    generation_model: str = "Qwen/Qwen2.5-32B-Instruct"
    generation_api_key: SecretStr | None = None

    #: Run the deterministic local adapters (hashed embeddings, lexical rerank) instead of
    #: calling a model at all. This is what makes the stack runnable on a machine with no GPU
    #: node: without it, `KB_ENV=dev` demands a live embedding server and `KB_ENV=test` is not
    #: an answer, because it also redirects object storage to a local directory.
    #:
    #: The adapters it selects declare `semantic: false`, so an eval run made under it cannot
    #: be mistaken for a measurement of the real models. `Settings` refuses it outright in
    #: staging and production.
    deterministic_fallback: bool = False

    def leaves_network(self, model: str) -> bool:
        return model in self.external_models


class TemporalSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KB_TEMPORAL_", extra="ignore")

    host: str = "localhost:7233"
    namespace: str = "kb"
    task_queue: str = "kb-main"


class RetentionSettings(BaseSettings):
    """[OPEN]-4: Legal owes the schedule. Seeded conservatively at 10 years (INV-9)."""

    model_config = SettingsConfigDict(env_prefix="KB_RETENTION_", extra="ignore")

    default_years: int = 10
    regulatory_years: int = 10
    internal_normative_years: int = 10
    operational_years: int = 10
    customer_facing_years: int = 10


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="KB_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    env: Environment = "dev"
    #: Browser origins allowed to call this service cross-origin. Empty by default: a service
    #: with no browser talking to it directly should answer no preflight at all. The portal is
    #: served from its own origin (nginx on :5173) and calls portal-api on :8008, so that one
    #: pair needs it — without it the browser blocks every call and the screens are silently
    #: empty, which looks like missing data rather than a blocked request.
    cors_origins: list[str] = Field(default_factory=list)
    #: What this *deployment* serves. `external` is the DMZ: the process refuses to answer
    #: anything but the public surface, so bypassing the gateway does not reach the internal
    #: bot (INV-4). It is a deployment fact, not a per-request one.
    surface: Surface = "internal"
    service_name: str = "kb-service"
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"
    # Publish-to-searchable budget (INV-5). Exceeding it is an alert, not a silent lag.
    index_visibility_budget_seconds: int = 10

    db: DatabaseSettings = Field(default_factory=DatabaseSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    keyword: KeywordIndexSettings = Field(default_factory=KeywordIndexSettings)
    oidc: IdentitySettings = Field(default_factory=IdentitySettings)
    models: ModelGatewaySettings = Field(default_factory=ModelGatewaySettings)
    temporal: TemporalSettings = Field(default_factory=TemporalSettings)
    retention: RetentionSettings = Field(default_factory=RetentionSettings)
    #: Where the funnel lives for this deployment (INV-1). The DMZ points at its own
    #: instance, which connects to the database as the external-only role.
    retrieval_url: str = "http://retrieval-api:8000"

    @property
    def is_production(self) -> bool:
        return self.env in ("staging", "prod")

    @property
    def use_deterministic_models(self) -> bool:
        """Whether the adapters should be the local deterministic ones (INV-12).

        Two ways in, one meaning: the test environment, or a dev deployment that has no model
        node to call. Both produce output that declares `semantic: false`.
        """
        return self.env == "test" or self.models.deterministic_fallback


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    if settings.is_production and settings.oidc.allow_insecure_tokens:
        raise RuntimeError("KB_OIDC_ALLOW_INSECURE_TOKENS must never be set outside dev/test")
    if settings.is_production and settings.models.deterministic_fallback:
        raise RuntimeError("KB_MODEL_DETERMINISTIC_FALLBACK must never be set outside dev/test")
    return settings


def reset_settings_cache() -> None:
    """Tests only."""
    get_settings.cache_clear()
