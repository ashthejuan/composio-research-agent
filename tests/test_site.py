"""Unit tests for static site generator and exports (Phase 5, FR-11 / FR-12)."""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path

from agent import site


def test_parse_human_log():
    parsed = site.parse_human_log(Path("data/human_log.md"))
    assert parsed["total"] >= 20
    assert parsed["by_theme"]
    assert all({"when", "app", "reason", "action", "theme"} <= set(e) for e in parsed["entries"])


def test_build_site_outputs_and_structure(tmp_path):
    res = site.build_site(out_dir=tmp_path)
    assert res["records"] == 100
    index_html = (tmp_path / "index.html").read_text(encoding="utf-8")
    apps_json = json.loads((tmp_path / "apps.json").read_text(encoding="utf-8"))
    llms_txt = (tmp_path / "llms.txt").read_text(encoding="utf-8")

    # Export files match 100 apps
    assert len(apps_json) == 100
    with (tmp_path / "apps.csv").open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 100
    assert "apps.json" in llms_txt and "apps.csv" in llms_txt and "Pass 2" in llms_txt

    # Embedded JSON dataset parses cleanly
    m = re.search(r'<script type="application/json" id="dataset">(.*?)</script>', index_html, re.S)
    assert m is not None
    embedded = json.loads(m.group(1).replace("<\\/", "</"))
    assert len(embedded["records"]) == 100
    assert embedded["analytics"]["sanity_checks"]["all_passed"] is True

    # All 8 FR-11 sections exist in order
    section_ids = ["headlines", "opportunities", "patterns", "explorer", "verification", "wrong", "architecture", "reproduce"]
    positions = [index_html.index(f'id="{sid}"') for sid in section_ids]
    assert positions == sorted(positions)

    # Scandinavian design guardrails: no uppercase CSS transform, no external CDN script required
    assert "text-transform: uppercase" not in index_html
    assert "cdnjs.cloudflare.com" not in index_html
    assert "human-corrected" in index_html.lower()
