"""The sign-off pack reports the system's actual state (M8 acceptance).

The pack is what Compliance and Legal sign. Its value is entirely in being *read from the
system* — so what is tested here is that it notices things: a document that is publicly
visible, an approval that is missing, a class that should not be public. A pack that always
looks clean would be worse than none.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import Engine

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[1]
EXPORT = ROOT / "scripts" / "export_signoff.py"


@pytest.fixture
def pack(tmp_path_factory: pytest.TempPathFactory, pristine_corpus: Engine) -> dict:
    output = tmp_path_factory.mktemp("signoff")
    result = subprocess.run(
        [sys.executable, str(EXPORT), "--output", str(output)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads((output / "external-surface-signoff.json").read_text(encoding="utf-8"))


def test_the_pack_lists_what_the_public_can_reach(pack: dict) -> None:
    assert pack["corpus"], "the pack claims the public bot can reach nothing"
    for item in pack["corpus"]:
        assert item["title"] and item["category_path"]
        assert item["chunks"] > 0, f"{item['title']} is public with no indexed text"


def test_the_pack_shows_no_internal_document(pack: dict) -> None:
    """The one finding that would stop a sign-off outright."""
    titles = " ".join(item["title"] for item in pack["corpus"]).lower()
    for internal in ("nhận biết khách hàng", "vận hành quầy", "canary", "hội đồng quản trị"):
        assert internal not in titles


def test_the_pack_names_the_prompt_it_was_generated_from(pack: dict) -> None:
    """A reviewer signs off on a text, not on a description of one."""
    assert len(pack["prompt"]["sha256"]) == 64
    rules = " ".join(pack["prompt"]["conduct_rules"])
    assert "Không cam kết" in rules
    assert "Không tư vấn tài chính" in rules
    assert "Không xử lý thông tin cá nhân" in rules


def test_the_pack_states_the_surface_configuration(pack: dict) -> None:
    surface = pack["surface"]
    assert surface["output_filter_required"] is True
    assert surface["graph_expansion"] is False
    assert surface["allowed_principal_kinds"] == ["external_bot"]
    assert surface["rate_limit_per_minute"] > 0


def test_the_pack_lists_every_control_with_the_file_that_implements_it(pack: dict) -> None:
    files = {control["file"] for control in pack["controls"]}
    assert "ops/dmz/external-role.sql" in files
    assert any("filters.py" in file for file in files)
    for control in pack["controls"]:
        assert (ROOT / control["file"]).exists(), f"{control['file']} does not exist"


def test_the_pack_reports_findings_rather_than_hiding_them(pack: dict) -> None:
    """The seeded corpus is inserted directly, so its approval trail is empty — and the pack
    says so. This is the mechanism that would catch a real document published without one."""
    assert isinstance(pack["warnings"], list)
    assert any("approval" in warning for warning in pack["warnings"])


def test_the_pack_records_what_is_still_open(pack: dict) -> None:
    assert any("[OPEN]" in item for item in pack["open_items"])
