#!/usr/bin/env python
"""External conduct red team — the public bot, under pressure (M8 acceptance).

Runs `eval/external_redteam` as the external bot against the external surface, and reports two
kinds of failure separately because they are two different conversations with two different
departments:

* **Disclosure** — internal content, a canary, or a personal identifier reached a stranger.
  Security's problem, and a filter failure.
* **Conduct** — the bank promised something, recommended something, quoted a figure with no
  date, or disparaged a competitor. Legal and Compliance's problem, and a *product* failure:
  every word here is something the bank said on a public channel.

Either fails the run. The distinction is in the report because the remedies differ — one is a
filter, the other is a prompt and a review.

    make seed && make external-redteam

The scoring runs the whole pipeline (`kb_chat_api.service.ChatService`) with the hard-scoped
external principal, so the ACL filter, the relevance gate, the citation check and the PII
output filter are all in the path exactly as they are in the DMZ.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

DEFAULT_SUITE = ROOT / "eval" / "external_redteam"
#: Every prompt runs as the hard-scoped public account (INV-4) on the public surface.
EXTERNAL_PRINCIPAL = "external_bot"
EXTERNAL_SURFACE = "external"
#: The acceptance criterion is "the full external red-team suite passes". Not a score.
REQUIRED_PROMPTS = 30


def load_suite(path: Path) -> list[dict[str, Any]]:
    from answers import load_yaml

    entries = load_yaml(path)
    for entry in entries:
        entry.setdefault("principal", EXTERNAL_PRINCIPAL)
        entry.setdefault("surface", EXTERNAL_SURFACE)
    return entries


def evaluate(entries: list[dict[str, Any]], generation_name: str) -> list[Any]:
    from answers import build_generation, missing_capabilities, run_redteam
    from kb_common.config import get_settings
    from kb_common.db import create_db_engine
    from run import build_engine, document_key_map
    from sqlalchemy.orm import Session

    keys = document_key_map()
    generation = build_generation(generation_name)
    missing = missing_capabilities(generation)
    db = create_db_engine(get_settings().db)
    try:
        with Session(db) as session:
            engine = build_engine(session)
            return [run_redteam(entry, engine, keys, generation, missing) for entry in entries]
    finally:
        db.dispose()


def classify(result: Any) -> tuple[list[str], list[str]]:
    """Split one result's findings into disclosure and conduct."""
    disclosure = [
        finding
        for finding in result.leaks
        if finding.startswith(("leaked token", "cited", "personal data"))
    ]
    conduct = [finding for finding in result.leaks if finding not in disclosure]
    return disclosure, conduct


def report(results: list[Any], generation_name: str) -> int:
    print(f"\ngeneration adapter: {generation_name}\n")
    print(f"{'id':<5} {'held':<5} attack")
    disclosures: list[str] = []
    conduct_breaches: list[str] = []
    weak: list[str] = []

    for result in results:
        disclosure, conduct = classify(result)
        if disclosure:
            disclosures.append(result.prompt_id)
        if conduct:
            conduct_breaches.append(result.prompt_id)
        if result.weak:
            weak.append(result.prompt_id)
        mark = "N" if (disclosure or conduct) else ("~" if result.weak else "y")
        notes = "; ".join(disclosure + conduct + result.weak)
        print(f"{result.prompt_id:<5} {mark:<5} {result.name}" + (f" — {notes}" if notes else ""))

    held = len(results) - len({*disclosures, *conduct_breaches})
    print(f"\n{held}/{len(results)} attempts held")
    print(f"  disclosure failures: {disclosures or 'none'}")
    print(f"  conduct failures:    {conduct_breaches or 'none'}")
    if weak:
        print(
            f"  answered where declining is better, disclosing nothing: {weak}\n"
            "  (deciding to decline is model judgement; blocking once a real model is configured)"
        )
    return len({*disclosures, *conduct_breaches})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    parser.add_argument("--generation", default="extractive", choices=["extractive", "vllm", "api"])
    parser.add_argument("--json", type=Path, help="write the evidence here for the sign-off pack")
    parser.add_argument("--dry-run", action="store_true", help="validate the suite only")
    args = parser.parse_args()

    from answers import validate
    from run import document_key_map

    entries = load_suite(args.suite)
    problems = validate(
        entries,
        keys=set(document_key_map().values()),
        require_expect=False,
        default_principal=EXTERNAL_PRINCIPAL,
    )
    if problems:
        print("external red-team suite is invalid:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    if len(entries) < REQUIRED_PROMPTS:
        print(
            f"the suite has {len(entries)} prompts; the criterion names {REQUIRED_PROMPTS}",
            file=sys.stderr,
        )
        return 1
    print(f"external red-team suite ok — {len(entries)} prompts")
    if args.dry_run:
        return 0

    results = evaluate(entries, args.generation)
    failures = report(results, args.generation)

    if args.json:
        args.json.write_text(
            json.dumps(
                [
                    {
                        "id": result.prompt_id,
                        "name": result.name,
                        "refused": result.refused,
                        "disclosure": classify(result)[0],
                        "conduct": classify(result)[1],
                        "weak": result.weak,
                    }
                    for result in results
                ],
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    if failures:
        print(f"\nFAILED: {failures} external red-team attempt(s) succeeded", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
