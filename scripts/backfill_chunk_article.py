"""Fill `chunks.article`, `chunks.anchor` and `chunks.subject_key` on older rows.

M9b introduced these columns (migrations 0006 and 0007), and the plan called for a corpus rechunk to
populate them. A rechunk is not needed, and that is worth saying plainly because the cheaper
route is also the safer one: **all three are pure functions of `section_path`, which is
already stored on every chunk row.** So this derives them in place, using the same two
functions the chunker calls, and never touches storage, the embedding model, or the publish
transaction.

What that buys, against `PublishService.rechunk` over 3,000 documents:

* no re-embedding, so no GPU time and no cost;
* no reads of the stored KBDoc, so a document whose derived JSON has been archived still gets
  its columns;
* chunk ids do not change, so nothing anchored on them is disturbed mid-backfill;
* it is idempotent and interruptible — re-running it costs one scan and changes nothing.

The values agree with what a rechunk would produce *by construction*, because both call
`kb_indexer.chunker.article_number` and `.subject_key`. A test asserts that rather than
trusting the sentence.

Tombstoned rows are filled too. They are archive, but `as_of` reaches them, and an article
number is as true of an old version as of the current one.
"""

from __future__ import annotations

from kb_common.db import create_db_engine
from kb_common.logging import configure_logging, get_logger
from kb_indexer.chunker import article_number, split_section_path, subject_key
from kb_vntext.sections import build_anchor
from sqlalchemy import text
from sqlalchemy.orm import Session

log = get_logger(__name__)

#: Rows per transaction. Small enough that an interrupted run leaves a short redo, large
#: enough that a 3,000-document corpus is not 100,000 round trips.
BATCH = 2000


def backfill(session: Session, *, batch: int = BATCH) -> tuple[int, int]:
    """Returns (rows examined, rows changed)."""
    examined = 0
    changed = 0
    last_id = "00000000-0000-0000-0000-000000000000"

    while True:
        rows = session.execute(
            text(
                """
                SELECT id, section_path, article, anchor, subject_key
                FROM chunks
                WHERE id > CAST(:last AS uuid)
                ORDER BY id
                LIMIT :batch
                """
            ),
            {"last": last_id, "batch": batch},
        ).all()
        if not rows:
            break

        for row in rows:
            examined += 1
            last_id = str(row.id)
            path = split_section_path(row.section_path)
            article = article_number(path)
            anchor = build_anchor(path)
            subject = subject_key(path)
            if article == row.article and anchor == row.anchor and subject == row.subject_key:
                continue
            session.execute(
                text(
                    "UPDATE chunks SET article = :article, anchor = :anchor, "
                    "subject_key = :subject WHERE id = :id"
                ),
                {"article": article, "anchor": anchor, "subject": subject, "id": row.id},
            )
            changed += 1
        session.commit()
        log.info("chunk_backfill_batch", extra={"examined": examined, "changed": changed})

    return examined, changed


def main() -> None:
    configure_logging("backfill-chunk-article", "INFO", "text")
    with Session(create_db_engine()) as session:
        examined, changed = backfill(session)
    print(f"examined {examined} chunks")
    print(f"filled {changed}")
    print("re-run at any time; it is idempotent")


if __name__ == "__main__":
    main()
