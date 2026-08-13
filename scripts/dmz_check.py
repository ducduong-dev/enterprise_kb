#!/usr/bin/env python
"""DMZ isolation check — prove the public surface is contained (M8, INV-4).

The DMZ's safety rests on four controls that fail independently. This asks each of them
directly, against the running profile, instead of trusting the compose file to have meant what
it says:

1. **The database role.** Connect as `kb_external` and try to read internal rows, a canary, and
   the audit trail. All three must come back empty or refused, and a write must be refused.
2. **The deployment surface.** Ask the DMZ chat process for the *internal* surface. It must
   refuse — not because the gateway did not route it, but because that process will not serve
   it (`KB_SURFACE=external`).
3. **The gateway.** Ask for paths that exist on the service but must not be public. 404, not a
   proxied answer.
4. **The network.** From inside a DMZ container, try to reach MinIO, Temporal, the portal and
   the internal chat. Every one must fail to resolve or connect — and the model proxy, its one
   permitted bridge besides the database, must succeed (ADR-0022 as amended).

Run it after `make dmz`, and again before any external sign-off:

    uv run python scripts/dmz_check.py

Exits non-zero on the first control that does not hold. Every check names what it proved, so
the output is evidence a reviewer can read — it is quoted into the sign-off pack.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CANARY_TOKENS = ("CANARY-ALPHA-7F3D", "CANARY-BRAVO-2C91", "CANARY-CHARLIE-E5A8")
#: Hosts the DMZ must not be able to reach. Not an exhaustive list of the platform — an
#: exhaustive list of what someone standing in the DMZ would try next. The model *servers* are
#: on it deliberately: the DMZ reaches models through the proxy or not at all (ADR-0024).
FORBIDDEN_HOSTS = (
    "minio",
    "temporal",
    "portal-api",
    "chat-api",
    "registry",
    "indexer",
    "vllm-generation",
    "vllm-vlm",
)


@dataclass
class Checks:
    results: list[tuple[str, bool, str]] = field(default_factory=list)

    def record(self, name: str, passed: bool, detail: str = "") -> None:
        self.results.append((name, passed, detail))
        print(f"  [{'ok  ' if passed else 'FAIL'}] {name}{f' — {detail}' if detail else ''}")

    @property
    def failed(self) -> list[str]:
        return [name for name, passed, _ in self.results if not passed]


def psql(container: str, user: str, sql: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "docker",
            "exec",
            container,
            "psql",
            "-U",
            user,
            "-d",
            os.environ.get("KB_DB_NAME", "kb"),
            "-tAc",
            sql,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def check_database_role(checks: Checks, container: str) -> None:
    internal = psql(
        container, "kb_external", "SELECT count(*) FROM chunks WHERE visibility <> 'external'"
    )
    checks.record(
        "the DMZ role cannot see internal chunks",
        internal.returncode == 0 and internal.stdout.strip() == "0",
        f"rows={internal.stdout.strip() or internal.stderr.strip()[:60]}",
    )

    canaries = psql(
        container,
        "kb_external",
        "SELECT count(*) FROM chunks WHERE "
        + " OR ".join(f"text LIKE '%{t}%'" for t in CANARY_TOKENS),
    )
    checks.record(
        "the DMZ role cannot see a canary",
        canaries.returncode == 0 and canaries.stdout.strip() == "0",
        f"rows={canaries.stdout.strip() or canaries.stderr.strip()[:60]}",
    )

    audit = psql(container, "kb_external", "SELECT count(*) FROM audit_log")
    checks.record(
        "the DMZ role cannot read the audit trail",
        audit.returncode != 0 and "permission denied" in audit.stderr,
        audit.stderr.strip().splitlines()[0] if audit.stderr else "unexpectedly allowed",
    )

    write = psql(container, "kb_external", "UPDATE chunks SET text = text")
    checks.record(
        "the DMZ role cannot write",
        write.returncode != 0 and "permission denied" in write.stderr,
        write.stderr.strip().splitlines()[0] if write.stderr else "unexpectedly allowed",
    )


#: Asks the DMZ process for the internal surface from inside its own container, so the answer
#: is the process's and not the gateway's.
_INTERNAL_PROBE = """
import json, urllib.error, urllib.request
body = json.dumps({"messages": [{"role": "user", "content": "x"}]}).encode()
request = urllib.request.Request(
    "http://localhost:8000/v1/chat/internal",
    data=body,
    headers={"Content-Type": "application/json", "Authorization": "Bearer x"},
)
try:
    urllib.request.urlopen(request, timeout=10)
    print("ALLOWED")
except urllib.error.HTTPError as exc:
    print(exc.code)
"""


def check_deployment_surface(checks: Checks, container: str) -> None:
    internal = subprocess.run(
        ["docker", "exec", "-i", container, "python", "-"],
        input=_INTERNAL_PROBE,
        capture_output=True,
        text=True,
        check=False,
    )
    code = internal.stdout.strip().splitlines()[-1] if internal.stdout.strip() else ""
    checks.record(
        "the DMZ process refuses the internal surface",
        code in {"401", "403"},
        f"status={code or internal.stderr.strip()[:60]}",
    )


def check_gateway(checks: Checks, gateway_url: str) -> None:
    import urllib.error
    import urllib.request

    def status(path: str, method: str = "GET") -> int:
        request = urllib.request.Request(f"{gateway_url}{path}", method=method)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return int(response.status)
        except urllib.error.HTTPError as exc:
            return int(exc.code)
        except OSError:
            return 0

    for path in ("/v1/chat/internal", "/v1/retrieve", "/v1/documents", "/metrics", "/docs"):
        code = status(path)
        checks.record(f"the gateway does not route {path}", code in (404, 405), f"status={code}")

    checks.record(
        "the gateway routes the public surface", status("/v1/chat/external") in (401, 405, 422), ""
    )


def check_network(checks: Checks, container: str) -> None:
    # The bridge that must work. A DMZ that cannot reach the proxy answers nothing, and the
    # symptom — every public question refused — looks exactly like a corpus problem.
    reachable = subprocess.run(
        ["docker", "exec", "-i", container, "python", "-"],
        input=(
            "import socket\n"
            "socket.setdefaulttimeout(3)\n"
            "socket.create_connection(('litellm', 4000))\n"
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    checks.record(
        "the DMZ can reach the model proxy",
        reachable.returncode == 0,
        "connected" if reachable.returncode == 0 else "unreachable — the bot cannot answer",
    )

    for host in FORBIDDEN_HOSTS:
        probe = subprocess.run(
            ["docker", "exec", "-i", container, "python", "-"],
            input=(
                "import socket\n"
                "socket.setdefaulttimeout(3)\n"
                f"socket.create_connection(({host!r}, 8000))\n"
            ),
            capture_output=True,
            text=True,
            check=False,
        )
        checks.record(
            f"the DMZ cannot reach {host}",
            probe.returncode != 0,
            "connected" if probe.returncode == 0 else "unreachable",
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-container", default="kb-platform-postgres-1")
    parser.add_argument("--dmz-container", default="kb-platform-dmz-chat-api-1")
    parser.add_argument("--gateway", default="http://localhost:8443")
    parser.add_argument("--json", type=Path, help="write the evidence here for the sign-off pack")
    parser.add_argument(
        "--skip",
        default="",
        help="comma-separated: database,surface,gateway,network (for partial environments)",
    )
    args = parser.parse_args()
    skip = {item.strip() for item in args.skip.split(",") if item.strip()}

    checks = Checks()
    print("DMZ isolation")
    if "database" not in skip:
        check_database_role(checks, args.db_container)
    if "surface" not in skip:
        check_deployment_surface(checks, args.dmz_container)
    if "gateway" not in skip:
        check_gateway(checks, args.gateway)
    if "network" not in skip:
        check_network(checks, args.dmz_container)

    if args.json:
        args.json.write_text(
            json.dumps(
                [
                    {"check": name, "passed": passed, "detail": detail}
                    for name, passed, detail in checks.results
                ],
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    if checks.failed:
        print(f"\nDMZ CHECK FAILED: {', '.join(checks.failed)}", file=sys.stderr)
        return 1
    print(f"\ndmz check passed — {len(checks.results)} controls verified")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
