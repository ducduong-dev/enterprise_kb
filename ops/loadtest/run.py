#!/usr/bin/env python
"""Load driver — 50 concurrent users against the running stack (M7).

Drives the surfaces a user actually touches, in the proportions a working day produces:
search dominates, citation clicks follow the searches, and chat is a minority of requests
that costs the most per call. A load profile of one endpoint at full rate measures a number
nobody experiences.

    make dev-all && make seed
    uv run python ops/loadtest/run.py --concurrency 50 --duration 900

Every request carries a fixture user's token, so the ACL filter is built and applied exactly
as in production — the load is *through* the funnel (INV-1/INV-2), not around it. Different
workers use different principals, which is deliberate: a run where every request resolves to
one cached filter would be measuring a cache nobody has.

Exits non-zero when the error rate or a p95 budget is breached, so it can gate a release.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import jwt

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

#: Budgets. Retrieval is a keystroke away from a user; chat is understood to be slow.
BUDGETS_MS: dict[str, float] = {"search": 800.0, "citation": 500.0, "chat": 5000.0}
#: Share of requests per surface. Sums to 1.0.
MIX: dict[str, float] = {"search": 0.7, "citation": 0.2, "chat": 0.1}
MAX_ERROR_RATE = 0.01

QUERIES = [
    "tỷ lệ an toàn vốn tối thiểu",
    "ty le an toan von",
    "hệ số rủi ro tín dụng",
    "phí duy trì tài khoản",
    "quy trình nhận biết khách hàng",
    "kiểm quỹ cuối ngày",
    "41/2016/TT-NHNN",
    "internal capital buffer",
]
CITATIONS = [
    "Điều 6.1, TT 41/2016/TT-NHNN",
    "Điều 12.2, TT 41/2016/TT-NHNN",
    "Mục 3, QĐ 114/2023",
]
#: Fixture users, mirroring `kb_authz.fixtures` and the Keycloak dev realm.
USERS = [
    ("u-retail-staff", ["/dept/retail"]),
    ("u-branch-teller", ["/dept/retail", "/branch/hcm-01"]),
    ("u-compliance-officer", ["/dept/compliance"]),
    ("u-legal-counsel", ["/dept/legal"]),
    ("u-it-engineer", ["/dept/it"]),
]


@dataclass
class Samples:
    latencies: list[float] = field(default_factory=list)
    errors: int = 0
    statuses: dict[int, int] = field(default_factory=lambda: defaultdict(int))

    def percentile(self, fraction: float) -> float:
        if not self.latencies:
            return 0.0
        ordered = sorted(self.latencies)
        index = min(len(ordered) - 1, int(len(ordered) * fraction))
        return ordered[index] * 1000

    @property
    def p50(self) -> float:
        return statistics.median(self.latencies) * 1000 if self.latencies else 0.0


def token_for(subject: str, groups: list[str], *, secret: str, issuer: str, audience: str) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": subject,
            "iss": issuer,
            "aud": audience,
            "iat": now,
            "exp": now + timedelta(hours=2),
            "preferred_username": subject,
            "groups": groups,
            "realm_access": {"roles": []},
            "azp": "kb-portal",
        },
        secret,
        algorithm="HS256",
    )


def pick(mix: dict[str, float]) -> str:
    roll = random.random()
    running = 0.0
    for surface, share in mix.items():
        running += share
        if roll <= running:
            return surface
    return "search"


async def one_request(
    client: httpx.AsyncClient, surface: str, token: str, results: dict[str, Samples]
) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    started = time.perf_counter()
    try:
        if surface == "search":
            response = await client.post(
                "/v1/retrieve",
                json={"query": random.choice(QUERIES), "top_k": 10},
                headers=headers,
            )
        elif surface == "citation":
            response = await client.post(
                "/v1/citation-lookup",
                json={"citation": random.choice(CITATIONS)},
                headers=headers,
            )
        else:
            response = await client.post(
                "/v1/chat/internal",
                json={"messages": [{"role": "user", "content": random.choice(QUERIES)}]},
                headers=headers,
            )
    except httpx.HTTPError:
        results[surface].errors += 1
        return

    elapsed = time.perf_counter() - started
    sample = results[surface]
    sample.statuses[response.status_code] += 1
    if response.status_code >= 500 or response.status_code == 429:
        # A 429 under the target concurrency means the rate limit is set below the load the
        # system is supposed to carry — a finding, not a client error to ignore.
        sample.errors += 1
        return
    if response.status_code >= 400:
        sample.errors += 1
        return
    sample.latencies.append(elapsed)


async def worker(
    index: int,
    deadline: float,
    results: dict[str, Samples],
    *,
    retrieval_url: str,
    chat_url: str,
    tokens: list[str],
    mix: dict[str, float],
    think_time: float,
) -> None:
    token = tokens[index % len(tokens)]
    async with (
        httpx.AsyncClient(base_url=retrieval_url, timeout=30.0) as retrieval,
        httpx.AsyncClient(base_url=chat_url, timeout=60.0) as chat,
    ):
        while time.perf_counter() < deadline:
            surface = pick(mix)
            client = chat if surface == "chat" else retrieval
            await one_request(client, surface, token, results)
            if think_time:
                await asyncio.sleep(random.uniform(0, think_time))


async def drive(args: argparse.Namespace) -> dict[str, Samples]:
    from kb_common.config import get_settings

    oidc = get_settings().oidc
    tokens = [
        token_for(
            subject,
            groups,
            secret=args.secret,
            issuer=oidc.issuer,
            audience=oidc.audience,
        )
        for subject, groups in USERS
    ]
    results: dict[str, Samples] = {surface: Samples() for surface in MIX}
    deadline = time.perf_counter() + args.duration
    await asyncio.gather(
        *(
            worker(
                index,
                deadline,
                results,
                retrieval_url=args.retrieval_url,
                chat_url=args.chat_url,
                tokens=tokens,
                mix=MIX,
                think_time=args.think_time,
            )
            for index in range(args.concurrency)
        )
    )
    return results


def report(results: dict[str, Samples], *, duration: float, concurrency: int) -> int:
    print(f"\n{concurrency} concurrent users · {duration:.0f}s\n")
    print(f"{'surface':<10}{'requests':>10}{'errors':>8}{'p50 ms':>10}{'p95 ms':>10}{'p99 ms':>10}")
    failures: list[str] = []
    for surface, sample in results.items():
        total = len(sample.latencies) + sample.errors
        print(
            f"{surface:<10}{total:>10}{sample.errors:>8}"
            f"{sample.p50:>10.1f}{sample.percentile(0.95):>10.1f}{sample.percentile(0.99):>10.1f}"
        )
        if not total:
            continue
        error_rate = sample.errors / total
        if error_rate > MAX_ERROR_RATE:
            failures.append(f"{surface}: error rate {error_rate:.1%} above {MAX_ERROR_RATE:.0%}")
        budget = BUDGETS_MS[surface]
        if sample.percentile(0.95) > budget:
            failures.append(
                f"{surface}: p95 {sample.percentile(0.95):.0f} ms above the {budget:.0f} ms budget"
            )
        codes = ", ".join(f"{code}x{count}" for code, count in sorted(sample.statuses.items()))
        print(f"{'':<10}{codes}")

    if failures:
        print("\nFAILED:")
        for failure in failures:
            print(f"  {failure}")
        return 1
    print("\nall surfaces within budget")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--concurrency", type=int, default=50)
    parser.add_argument("--duration", type=float, default=900.0, help="seconds")
    parser.add_argument("--retrieval-url", default="http://localhost:8002")
    parser.add_argument("--chat-url", default="http://localhost:8003")
    parser.add_argument(
        "--think-time",
        type=float,
        default=0.5,
        help="seconds of idle between a worker's requests; 0 is a stress test, not a load test",
    )
    parser.add_argument(
        "--secret",
        default="kb-test-secret",
        help="dev/test signing secret; refused outside dev and test by get_settings()",
    )
    args = parser.parse_args()

    results = asyncio.run(drive(args))
    return report(results, duration=args.duration, concurrency=args.concurrency)


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
