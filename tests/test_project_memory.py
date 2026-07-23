from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from omg_cli.project_memory import (
    export_memory,
    import_memory,
    memory_path,
    rescan_memory,
    search_memory,
    upsert_fact,
)
from omg_cli.wiki import ingest, list_pages


def test_fact_store_is_lock_safe_searchable_deterministic_and_redacted(tmp_path) -> None:
    def write(index: int) -> None:
        upsert_fact(
            tmp_path,
            key=f"fact-{index}",
            value=f"value {index} token=secret-{index}",
            source="user",
            updated_at=f"2026-07-22T00:00:{index:02d}Z",
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(write, range(10)))
    exported = export_memory(tmp_path)
    assert [row["key"] for row in exported["facts"]] == sorted(
        row["key"] for row in exported["facts"]
    )
    assert "secret-" not in json.dumps(exported)
    assert len(search_memory(tmp_path, "value", limit=20)) == 10
    assert memory_path(tmp_path).is_file()


def test_corrupt_store_is_quarantined_and_import_is_idempotent(tmp_path) -> None:
    path = memory_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")
    upsert_fact(
        tmp_path,
        key="safe",
        value="restored",
        source="user",
        updated_at="2026-07-22T00:00:00Z",
    )
    assert list(path.parent.glob("facts.corrupt-*.json"))
    bundle = export_memory(tmp_path)
    assert import_memory(tmp_path, bundle) == import_memory(tmp_path, bundle)


def test_rescan_preserves_user_fact_and_wiki_index_is_deterministic(tmp_path) -> None:
    upsert_fact(
        tmp_path,
        key="architecture",
        value="user decision",
        source="user",
        updated_at="2026-07-22T00:00:00Z",
    )
    rescan_memory(
        tmp_path,
        [{"key": "architecture", "value": "scanner guess"}, {"key": "module", "value": "runtime"}],
        observed_at="2026-07-22T00:01:00Z",
    )
    facts = {row["key"]: row for row in export_memory(tmp_path)["facts"]}
    assert facts["architecture"]["value"] == "user decision"
    ingest(tmp_path, title="Zeta", body="z")
    ingest(tmp_path, title="Alpha", body="a")
    assert [row["slug"] for row in list_pages(tmp_path)] == ["alpha", "zeta"]
