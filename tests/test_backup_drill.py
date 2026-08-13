"""The backup is restorable (M7).

`scripts/backup_drill.py` does the work; this runs it, so "we can recover" is a claim CI
checks rather than a paragraph in a runbook. Skipped where neither the Postgres client tools
nor a database container are available — the drill needs `pg_dump`, and a test that silently
passes without running it would be worse than no test.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import Engine

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[1]
DRILL = ROOT / "scripts" / "backup_drill.py"


def can_dump() -> bool:
    return bool(shutil.which("pg_dump")) or bool(os.environ.get("KB_PG_DOCKER_CONTAINER"))


@pytest.mark.skipif(not can_dump(), reason="no pg_dump and no KB_PG_DOCKER_CONTAINER")
def test_the_backup_restores_into_a_working_corpus(seeded: Engine) -> None:
    result = subprocess.run(
        [sys.executable, str(DRILL)],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ},
    )
    assert result.returncode == 0, f"drill failed:\n{result.stdout}\n{result.stderr}"
    # The checks that matter are named in the output, so a passing run that skipped them is
    # visible here rather than being reported as success.
    for check in (
        "one canonical version per document",
        "live chunks belong to the canonical version",
        "retrieval answers after the restore",
        "the ACL filter survived the restore",
    ):
        assert check in result.stdout, f"the drill did not run the {check!r} check"
