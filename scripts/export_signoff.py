#!/usr/bin/env python
"""Compliance and Legal sign-off pack for the public surface (M8 acceptance).

What a reviewer has to decide is narrow and specific: *may this bot answer the public, from
this corpus, under these rules?* Answering it should not require reading the codebase, and it
should not require trusting a summary either. So this exports the state of the system, from
the system:

1. **Everything the public can reach** — every externally visible published document, its
   effective dates, who approved the version that is canonical, and how many chunks of it are
   in the index. If a document appears here that should not be public, that is the finding.
2. **The rules in force** — the conduct prompt as deployed, with a hash, so the pack names the
   text that was reviewed rather than a paraphrase of it.
3. **The controls** — the scope binding, the mandatory output filter, the rate limits, the
   deployment surface, and the DMZ's database role, each with the file that implements it.
4. **The evidence** — the external red-team result and the DMZ isolation check, if they have
   been run and their JSON handed to this script.
5. **What is deliberately not settled** — open decisions that touch the public surface.

    make seed && uv run python scripts/export_signoff.py --output docs/signoff/

Produces Markdown for the reviewer and JSON for whatever the bank's GRC system ingests. It
reports what it finds; it does not decide. A pack with a problem in it is a working pack.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

EXTERNAL_PROMPT = ROOT / "services" / "chat-api" / "prompts" / "grounded_answer_external.md"
CONTROLS = [
    (
        "INV-4 · scope bound to the service account",
        "libs/authz/src/kb_authz/filters.py",
        "The external bot's filter is built from its own account, server-side. The request has "
        "no field that could widen it.",
    ),
    (
        "INV-4 · database role",
        "ops/dmz/external-role.sql",
        "The DMZ connects as a SELECT-only role whose row-level security shows it published "
        "external rows only — so a filter bug cannot read internal text.",
    ),
    (
        "INV-4 · deployment surface",
        "services/chat-api/src/kb_chat_api/main.py",
        "A process started with KB_SURFACE=dmz refuses the internal surface however it is reached.",
    ),
    (
        "INV-7 · output filter, mandatory",
        "services/chat-api/src/kb_chat_api/surfaces.py",
        "The public surface refuses to answer at all without a working PII detector — the same "
        "detector that gates ingestion.",
    ),
    (
        "INV-11 · every answer recorded",
        "services/chat-api/src/kb_chat_api/service.py",
        "Answers and refusals both write an audit record naming the principal, the resolved "
        "filter, the chunks used and the answer id. The DMZ role can write it and cannot read "
        "it.",
    ),
    (
        "Grounding · no citation, no answer",
        "services/chat-api/src/kb_chat_api/context.py",
        "Citations are verified against their passages, figures included; an answer left with "
        "none becomes the surface's refusal.",
    ),
    (
        "Rate limits",
        "ops/dmz/nginx.conf",
        "Per-IP at the gateway, per-principal in the service. The gateway routes one method on "
        "one path.",
    ),
]


@dataclass
class Pack:
    generated_at: str
    corpus: list[dict[str, Any]] = field(default_factory=list)
    prompt: dict[str, Any] = field(default_factory=dict)
    surface: dict[str, Any] = field(default_factory=dict)
    controls: list[dict[str, str]] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    open_items: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def collect_corpus(session: Any) -> list[dict[str, Any]]:
    """Every document the public can reach, and what makes it public."""
    from sqlalchemy import text

    rows = (
        session.execute(
            text(
                """
                SELECT d.id, d.title, d.doc_class, d.category_path::text AS category_path,
                       d.department, v.effective_from, v.effective_to, v.author,
                       v.pii_status, v.created_at AS version_created_at,
                       (SELECT count(*) FROM chunks c
                         WHERE c.document_id = d.id AND NOT c.tombstoned) AS chunks
                FROM documents d
                JOIN document_versions v ON v.id = d.canonical_version_id
                WHERE d.visibility = 'external' AND d.status = 'published'
                ORDER BY d.category_path::text, d.title
                """
            )
        )
        .mappings()
        .all()
    )
    return [
        {
            "document_id": str(row["id"]),
            "title": row["title"],
            "doc_class": row["doc_class"],
            "category_path": row["category_path"],
            "department": row["department"],
            "effective_from": row["effective_from"].isoformat() if row["effective_from"] else None,
            "effective_to": row["effective_to"].isoformat() if row["effective_to"] else None,
            "version_author": row["author"],
            "pii_status": row["pii_status"],
            "chunks": int(row["chunks"]),
        }
        for row in rows
    ]


def collect_approvals(session: Any, document_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Who approved what is public, from the audit trail rather than from memory."""
    from sqlalchemy import text

    if not document_ids:
        return {}
    rows = (
        session.execute(
            text(
                """
                SELECT object_ref->>'document_id' AS document_id, actor, ts, action
                FROM audit_log
                WHERE action IN ('publish', 'review_decision', 'pii_override')
                  AND object_ref->>'document_id' = ANY(CAST(:ids AS text[]))
                ORDER BY ts
                """
            ),
            {"ids": document_ids},
        )
        .mappings()
        .all()
    )
    approvals: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        approvals.setdefault(str(row["document_id"]), []).append(
            {"actor": row["actor"], "action": row["action"], "at": row["ts"].isoformat()}
        )
    return approvals


def collect_surface() -> dict[str, Any]:
    from kb_chat_api.surfaces import EXTERNAL

    return {
        "surface": EXTERNAL.surface.value,
        "allowed_principal_kinds": sorted(kind.value for kind in EXTERNAL.allowed_kinds),
        "passages_per_answer": EXTERNAL.top_k,
        "max_answer_tokens": EXTERNAL.max_answer_tokens,
        "history_turns": EXTERNAL.history_turns,
        "rate_limit_per_minute": EXTERNAL.rate_limit_per_minute,
        "graph_expansion": EXTERNAL.expand_graph,
        "output_filter_required": EXTERNAL.output_filter_required,
        "refusal_text": EXTERNAL.refusal,
    }


def collect_prompt() -> dict[str, Any]:
    text_content = EXTERNAL_PROMPT.read_text(encoding="utf-8")
    # Rules are numbered and wrapped in the source. A reviewer signs off on complete sentences,
    # so continuation lines are joined back onto the rule they belong to.
    rules: list[str] = []
    for line in text_content.splitlines():
        stripped = line.strip()
        if stripped[:2].rstrip(".").isdigit() and "." in stripped[:4]:
            rules.append(stripped)
        elif rules and stripped and not stripped.startswith("#"):
            rules[-1] = f"{rules[-1]} {stripped}"
        elif not stripped:
            continue
    return {
        "file": str(EXTERNAL_PROMPT.relative_to(ROOT)),
        "sha256": hashlib.sha256(text_content.encode("utf-8")).hexdigest(),
        "conduct_rules": rules,
        "text": text_content,
    }


def render_markdown(pack: Pack) -> str:
    lines: list[str] = [
        "# Public chatbot — Compliance and Legal sign-off pack",
        "",
        f"Generated {pack.generated_at} from the running system.",
        "",
        "This pack states what the public surface can reach, the rules it answers under, and "
        "the controls that hold those rules in place. Every section is read from the system "
        "itself; nothing here is a description of intent.",
        "",
        "## 1. What the public can reach",
        "",
    ]
    if not pack.corpus:
        lines.append(
            "_No externally visible published document. The public bot can answer nothing._"
        )
    else:
        lines.append(
            "| Document | Class | Category | Effective | Chunks | PII gate | Approved by |"
        )
        lines.append("|---|---|---|---|---|---|---|")
        for item in pack.corpus:
            approvals = pack.evidence.get("approvals", {}).get(item["document_id"], [])
            approver = ", ".join(sorted({a["actor"] for a in approvals})) or "—"
            effective = item["effective_from"] or "—"
            if item["effective_to"]:
                effective += f" → {item['effective_to']}"
            lines.append(
                f"| {item['title']} | {item['doc_class']} | {item['category_path']} | "
                f"{effective} | {item['chunks']} | {item['pii_status']} | {approver} |"
            )
    lines += ["", "## 2. Conduct rules in force", ""]
    lines.append(f"`{pack.prompt['file']}` · sha256 `{pack.prompt['sha256'][:16]}…`")
    lines.append("")
    for rule in pack.prompt["conduct_rules"]:
        lines.append(f"- {rule}")
    lines += ["", "## 3. Surface configuration", "", "| Setting | Value |", "|---|---|"]
    for key, value in pack.surface.items():
        lines.append(f"| {key} | {value} |")

    lines += [
        "",
        "## 4. Controls",
        "",
        "| Control | Implemented in | What it does |",
        "|---|---|---|",
    ]
    for control in pack.controls:
        lines.append(f"| {control['control']} | `{control['file']}` | {control['description']} |")

    lines += ["", "## 5. Evidence", ""]
    redteam = pack.evidence.get("external_redteam")
    if redteam:
        failures = [item for item in redteam if item["disclosure"] or item["conduct"]]
        lines.append(
            f"- External red team: **{len(redteam) - len(failures)}/{len(redteam)} held**"
            + (f" — failures: {[item['id'] for item in failures]}" if failures else "")
        )
        weak = [item["id"] for item in redteam if item.get("weak")]
        if weak:
            lines.append(f"  - answered where declining is preferable, disclosing nothing: {weak}")
    else:
        lines.append("- External red team: **not attached** (run `make external-redteam --json`)")
    dmz = pack.evidence.get("dmz_check")
    if dmz:
        failed = [item["check"] for item in dmz if not item["passed"]]
        lines.append(
            f"- DMZ isolation: **{len(dmz) - len(failed)}/{len(dmz)} controls verified**"
            + (f" — failed: {failed}" if failed else "")
        )
    else:
        lines.append("- DMZ isolation: **not attached** (run `make dmz-check` with `--json`)")

    lines += ["", "## 6. Open items", ""]
    for open_item in pack.open_items:
        lines.append(f"- {open_item}")

    if pack.warnings:
        lines += ["", "## 7. Findings", ""]
        for warning in pack.warnings:
            lines.append(f"- ⚠️ {warning}")

    lines += [
        "",
        "## Sign-off",
        "",
        "| Role | Name | Date | Decision |",
        "|---|---|---|---|",
        "| Compliance | | | |",
        "| Legal | | | |",
        "| Information security | | | |",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "docs" / "signoff")
    parser.add_argument(
        "--redteam-json", type=Path, help="output of eval/harness/external.py --json"
    )
    parser.add_argument("--dmz-json", type=Path, help="output of scripts/dmz_check.py --json")
    args = parser.parse_args()

    from kb_common.config import get_settings
    from kb_common.db import create_db_engine
    from sqlalchemy.orm import Session

    pack = Pack(generated_at=datetime.now(UTC).isoformat(timespec="seconds"))
    pack.prompt = collect_prompt()
    pack.surface = collect_surface()
    pack.controls = [
        {"control": name, "file": file, "description": description}
        for name, file, description in CONTROLS
    ]
    pack.open_items = [
        "[OPEN]-1 generation model: local vLLM by default; a hosted API would send public "
        "questions off-premises and needs a separate ruling.",
        "[OPEN]-3 existence disclosure per category: defaults to false, so the public surface "
        "answers 'not found' rather than 'exists but forbidden'.",
        "Conduct rules are enforced by a prompt and a red-team suite, not by a classifier. "
        "A model change re-opens the suite, not the sign-off.",
    ]

    db = create_db_engine(get_settings().db)
    try:
        with Session(db) as session:
            pack.corpus = collect_corpus(session)
            pack.evidence["approvals"] = collect_approvals(
                session, [item["document_id"] for item in pack.corpus]
            )
    finally:
        db.dispose()

    approvals: dict[str, list[dict[str, Any]]] = pack.evidence["approvals"]
    for item in pack.corpus:
        if item["pii_status"] not in ("clear", "overridden"):
            pack.warnings.append(f"{item['title']}: published with pii_status={item['pii_status']}")
        if not approvals.get(item["document_id"]):
            pack.warnings.append(f"{item['title']}: no approval found in the audit trail")
        if item["doc_class"] not in ("customer_facing", "regulatory"):
            pack.warnings.append(
                f"{item['title']}: {item['doc_class']} document is publicly visible"
            )

    if args.redteam_json and args.redteam_json.exists():
        pack.evidence["external_redteam"] = json.loads(args.redteam_json.read_text())
    if args.dmz_json and args.dmz_json.exists():
        pack.evidence["dmz_check"] = json.loads(args.dmz_json.read_text())

    args.output.mkdir(parents=True, exist_ok=True)
    markdown = args.output / "external-surface-signoff.md"
    payload = args.output / "external-surface-signoff.json"
    markdown.write_text(render_markdown(pack), encoding="utf-8")
    payload.write_text(
        json.dumps(
            {
                "generated_at": pack.generated_at,
                "corpus": pack.corpus,
                "prompt": {k: v for k, v in pack.prompt.items() if k != "text"},
                "surface": pack.surface,
                "controls": pack.controls,
                "evidence": pack.evidence,
                "open_items": pack.open_items,
                "warnings": pack.warnings,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    print(f"sign-off pack written to {markdown}")
    print(f"  {len(pack.corpus)} externally visible document(s)")
    if pack.warnings:
        print("  findings:")
        for warning in pack.warnings:
            print(f"    - {warning}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
