#!/usr/bin/env python
"""Static enforcement of the architectural invariants that a type checker cannot see.

INV-1  Single retrieval funnel — only `retrieval-api` may import index clients or index
       adapters. If chat-api could query the index directly, every ACL guarantee would have
       a second, unreviewed path around it.
INV-12 Model calls behind ports — model clients (vLLM/OpenAI, PaddleOCR, transformers …) may
       only be imported inside an `adapters/` module. Services import ports.

Run: `make invariants` (also runs in CI, blocking).
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Clients that talk to an index directly. `pgvector` is deliberately absent: it supplies the
#: column *type* the ORM needs. Vector search through it is caught by VECTOR_SEARCH_MARKERS.
#: Search-engine clients. `opensearchpy` stays on the list although that engine lost the
#: bake-off (ADR-0021): the rule is about *any* index client outside an adapter, and the next
#: one to arrive should trip it too.
INDEX_MODULES = frozenset({"opensearchpy", "opensearch_dsl", "elasticsearch"})

#: pgvector distance operators/helpers. Issuing one of these is querying the vector index.
VECTOR_SEARCH_MARKERS = ("<=>", "<->", "<#>", "cosine_distance", "l2_distance", "max_inner_product")

#: Clients that talk to a model directly. `cv2` is deliberately absent: OpenCV preprocessing
#: is deterministic image processing, not a model call, and the plan places it inside the IDP
#: service. INV-12 is about swappable *inference*, not about every image library.
MODEL_MODULES = frozenset(
    {
        "openai",
        "anthropic",
        "vllm",
        "transformers",
        "sentence_transformers",
        "paddleocr",
        "paddle",
        "torch",
        "FlagEmbedding",
    }
)

#: Object storage clients — same reasoning as models: behind StoragePort only.
STORAGE_MODULES = frozenset({"boto3", "botocore", "minio"})

#: Only these packages may reach an index. The indexer writes; retrieval-api reads.
INDEX_ALLOWED_PACKAGES = frozenset({"kb_retrieval_api", "kb_indexer"})

#: The shared index adapters. They live in `kb_ports.adapters` so there is one implementation
#: of the ACL-in-the-query contract, but *using* one is still restricted to the funnel: any
#: other service importing these has found a second read path around retrieval-api (INV-1).
INDEX_ADAPTER_MODULES = frozenset(
    {
        "kb_ports.adapters.pg_search_index",
        "kb_ports.adapters.pgvector_index",
        "kb_ports.adapters.postgres_fts_index",
    }
)

#: Ports themselves may name the clients in type-only contexts; adapters implement them.
ADAPTER_PATH_MARKERS = ("adapters", "adapter.py")

EXEMPT_DIRS = ("tests", "eval", "scripts", ".venv", "node_modules", "ops/alembic")


@dataclass(frozen=True)
class Violation:
    path: Path
    line: int
    module: str
    invariant: str
    reason: str

    def __str__(self) -> str:
        rel = self.path.relative_to(ROOT)
        return f"{rel}:{self.line}: {self.invariant}: imports {self.module!r} — {self.reason}"


def imported_paths(tree: ast.AST) -> list[tuple[str, int]]:
    """Fully-qualified module paths, for rules that care about a specific module."""
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append((node.module, node.lineno))
    return found


def imported_modules(tree: ast.AST) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((alias.name.split(".")[0], node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append((node.module.split(".")[0], node.lineno))
    return found


def is_adapter(path: Path) -> bool:
    parts = {p.lower() for p in path.parts}
    return bool(parts & set(ADAPTER_PATH_MARKERS)) or path.name.endswith("_adapter.py")


def owning_package(path: Path) -> str:
    """The distribution package a source file belongs to (`kb_chat_api`, `kb_ports`, …)."""
    parts = path.relative_to(ROOT).parts
    if "src" in parts:
        return parts[parts.index("src") + 1]
    return parts[0]


def check_file(path: Path) -> list[Violation]:
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:  # pragma: no cover - surfaced by ruff first
        return [Violation(path, exc.lineno or 0, "<unparseable>", "SYNTAX", str(exc))]

    package = owning_package(path)
    adapter = is_adapter(path)
    violations: list[Violation] = []

    for module, line in imported_modules(tree):
        if module in INDEX_MODULES and package not in INDEX_ALLOWED_PACKAGES and not adapter:
            violations.append(
                Violation(
                    path,
                    line,
                    module,
                    "INV-1",
                    "all reads funnel through retrieval-api; only it and the indexer may "
                    "touch an index client",
                )
            )
        if module in MODEL_MODULES and not adapter:
            violations.append(
                Violation(
                    path,
                    line,
                    module,
                    "INV-12",
                    "model clients belong in an adapters/ module behind a port",
                )
            )
        if module in STORAGE_MODULES and not adapter:
            violations.append(
                Violation(
                    path,
                    line,
                    module,
                    "INV-12",
                    "object storage belongs behind StoragePort in an adapters/ module",
                )
            )

    if package not in INDEX_ALLOWED_PACKAGES and package != "kb_ports":
        for module, line in imported_paths(tree):
            if module in INDEX_ADAPTER_MODULES:
                violations.append(
                    Violation(
                        path,
                        line,
                        module,
                        "INV-1",
                        "only retrieval-api (read) and the indexer (write) may use an index "
                        "adapter; everything else calls retrieval-api",
                    )
                )

    if package not in INDEX_ALLOWED_PACKAGES and not adapter:
        for lineno, text in enumerate(source.splitlines(), start=1):
            marker = next((m for m in VECTOR_SEARCH_MARKERS if m in text), None)
            if marker is not None:
                violations.append(
                    Violation(
                        path,
                        lineno,
                        marker,
                        "INV-1",
                        "vector search must go through retrieval-api, not a local query",
                    )
                )
    return violations


def source_files() -> list[Path]:
    files: list[Path] = []
    for base in ("libs", "services"):
        for path in (ROOT / base).rglob("*.py"):
            rel = path.relative_to(ROOT).as_posix()
            if any(f"/{d}/" in f"/{rel}" for d in EXEMPT_DIRS):
                continue
            files.append(path)
    return sorted(files)


def main() -> int:
    violations = [v for path in source_files() for v in check_file(path)]
    if violations:
        print("Architectural invariant violations:\n", file=sys.stderr)
        for violation in violations:
            print(f"  {violation}", file=sys.stderr)
        print(
            f"\n{len(violations)} violation(s). These are architecture, not style: fix the "
            "import, do not add an exception.",
            file=sys.stderr,
        )
        return 1
    print(f"invariants ok — {len(source_files())} files checked (INV-1, INV-12)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
