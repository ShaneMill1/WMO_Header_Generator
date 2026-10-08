"""SQLite storage for the generated registry.

The registry is build output: ``build_registry.py`` turns the pinned sources in
``sources.json`` into per-source *manifests* (non-row data: code tables, grid
specs, CCCC lists, abbreviation maps) and *entries* (the row data: notice,
parm, and radar records). This module stores both in a single SQLite file,
replacing the earlier ``*.manifest.json`` + ``*.entries.jsonl.gz`` layout.

The shape the resolver consumes is unchanged: :func:`read_db` returns
``(manifest, entries)`` pairs, exactly what ``Registry`` already expects from a
``LoadedSource``. The database is just a different container for the same build
output -- ``sources.json`` remains the sole hand-written, pinned input, and the
build-time integrity checks are untouched.

Schema::

    meta(key TEXT PRIMARY KEY, value TEXT)
        schema_version, built_at

    source(id TEXT PRIMARY KEY, kind TEXT, entry_count INTEGER, manifest JSON)
        one row per registry source; `manifest` is the per-source manifest dict

    entry(rowid INTEGER PRIMARY KEY, source_id TEXT, kind TEXT, data JSON)
        one row per registry entry; `data` is the entry dict
        indexed by source_id

Entries stay JSON rather than being shredded into typed columns: the three
entry kinds (notice / parm / radar) have different fields, matching is done in
Python against the full dict, and keeping them as JSON means the schema does not
have to change when a parser gains a field. The relational win is at the
source/entry grain (query, count, and load per source), not per field.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable

SCHEMA_VERSION = "1"

_SCHEMA = """
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE source (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    entry_count INTEGER NOT NULL,
    manifest    TEXT NOT NULL          -- JSON
);
CREATE TABLE entry (
    rowid     INTEGER PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES source(id),
    kind      TEXT NOT NULL,
    data      TEXT NOT NULL            -- JSON
);
CREATE INDEX idx_entry_source ON entry(source_id);
"""


def write_db(path: Path, built_sources: Iterable[dict]) -> None:
    """Write all built sources to a fresh SQLite registry at ``path``.

    ``built_sources`` is an iterable of ``{"manifest": dict, "entries": list}``.
    Any existing file is replaced, so a build always produces a clean database.
    """
    path = Path(path)
    if path.exists():
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path)
    try:
        conn.executescript(_SCHEMA)
        conn.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?)",
            ("schema_version", SCHEMA_VERSION),
        )
        for built in built_sources:
            manifest = built["manifest"]
            entries = built["entries"]
            sid = manifest["source"]["id"]
            kind = manifest["source"]["kind"]
            conn.execute(
                "INSERT INTO source(id, kind, entry_count, manifest) "
                "VALUES (?, ?, ?, ?)",
                (sid, kind, len(entries), json.dumps(manifest)),
            )
            conn.executemany(
                "INSERT INTO entry(source_id, kind, data) VALUES (?, ?, ?)",
                ((sid, kind, json.dumps(e, separators=(",", ":"))) for e in entries),
            )
        conn.commit()
    finally:
        conn.close()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='source'"
    ).fetchone()
    if not exists:
        conn.executescript(_SCHEMA)
        conn.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?)",
            ("schema_version", SCHEMA_VERSION),
        )


def upsert_source(path: Path, built: dict) -> None:
    """Insert or replace a single source (and its entries) in the registry.

    Used for incremental ``--source`` builds: it rewrites just that source's
    rows, leaving the other sources intact, so rebuilding one source does not
    require rebuilding the whole registry.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        _ensure_schema(conn)
        manifest = built["manifest"]
        entries = built["entries"]
        sid = manifest["source"]["id"]
        kind = manifest["source"]["kind"]
        conn.execute("DELETE FROM entry WHERE source_id = ?", (sid,))
        conn.execute("DELETE FROM source WHERE id = ?", (sid,))
        conn.execute(
            "INSERT INTO source(id, kind, entry_count, manifest) VALUES (?, ?, ?, ?)",
            (sid, kind, len(entries), json.dumps(manifest)),
        )
        conn.executemany(
            "INSERT INTO entry(source_id, kind, data) VALUES (?, ?, ?)",
            ((sid, kind, json.dumps(e, separators=(",", ":"))) for e in entries),
        )
        conn.commit()
    finally:
        conn.close()


def read_db(path: Path):
    """Return a list of ``(manifest, entries)`` pairs from a SQLite registry.

    The caller wraps each pair in whatever record type it uses (the resolver
    wraps them in ``LoadedSource``). Sources are returned in id order so loading
    is deterministic.
    """
    path = Path(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        out = []
        for srow in conn.execute(
            "SELECT id, manifest FROM source ORDER BY id"
        ).fetchall():
            manifest = json.loads(srow["manifest"])
            entries = [
                json.loads(erow["data"])
                for erow in conn.execute(
                    "SELECT data FROM entry WHERE source_id = ? ORDER BY rowid",
                    (srow["id"],),
                )
            ]
            out.append((manifest, entries))
        return out
    finally:
        conn.close()


def db_exists(path: Path) -> bool:
    return Path(path).is_file()
