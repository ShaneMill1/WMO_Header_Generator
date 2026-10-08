"""Build the WMO header registry from pinned authoritative sources.

Reads ``registry/sources.json``, fetches each source at its pinned tag, hashes
it, parses the authoritative records, and writes a generated registry. Nothing
is hand-transcribed: every emitted entry cites the source file and line it came
from, so any header can be traced back and independently checked.

Integrity checks that abort the build (rather than emit a possibly-wrong header):

* every ``&GRIBIDS`` line must parse (parsed count == raw line count);
* every record's PDT must be interpretable under its declared template;
* every parm file must map to exactly one grid binding;
* the A1 (grid) character in every WMOHEAD must equal the grid binding's
  expected A1, which is independently published in NCEP ON-388 Appendix A
  Table A.2. If NCEP ever repoints a parm file at a different grid, this fails
  loudly instead of silently producing wrong headers.

Usage::

    python build_registry.py              # build all sources
    python build_registry.py --source ncep-gfs-awips
    python build_registry.py --cache-dir /tmp/wmo_src
"""

from __future__ import annotations

import argparse
import ast
import csv
import fnmatch
import hashlib
import io
import json
import re
import sys
import tarfile
import urllib.request
from pathlib import Path

import pypdf

from nws_notice import (
    extract_cccc,
    find_conflicts,
    mark_conflicts,
    parse_html_product_annotations,
    parse_notice,
)
from tocgrib2_parm import parse_text, validate
import registry_db

HERE = Path(__file__).parent
REGISTRY_DIR = HERE / "registry"
SOURCES_JSON = REGISTRY_DIR / "sources.json"
REGISTRY_DB = REGISTRY_DIR / "registry.db"


class BuildError(RuntimeError):
    """Raised when a source fails an integrity check."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalize_name(name: str) -> str:
    """Case/separator-insensitive key. Must match wmo_header.normalize_name."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def fetch_tarball(url: str, cache_dir: Path) -> Path:
    """Download a source tarball (cached by URL hash)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    dest = cache_dir / (sha256_bytes(url.encode())[:16] + ".tar.gz")
    if dest.exists():
        print(f"  cached: {dest}")
        return dest
    print(f"  fetching: {url}")
    with urllib.request.urlopen(url) as resp, dest.open("wb") as fh:
        fh.write(resp.read())
    return dest


def read_parm_files(tarball: Path, parm_dir: str) -> dict:
    """Extract {filename: text} for files under ``parm_dir`` in the tarball."""
    out: dict = {}
    with tarfile.open(tarball, "r:gz") as tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            # Paths look like '<repo>-<tag>/parm/wmo/<file>'
            parts = member.name.split("/")
            if len(parts) < 2:
                continue
            rel = "/".join(parts[1:])
            if not rel.startswith(parm_dir.rstrip("/") + "/"):
                continue
            name = parts[-1]
            fh = tf.extractfile(member)
            if fh is None:
                continue
            out[name] = fh.read().decode("utf-8", errors="replace")
    return out


def match_grid(filename: str, grids: list, unbound: list):
    """Return (grid_dict_or_None, unbound_reason_or_None) for a parm filename."""
    hits = [g for g in grids if fnmatch.fnmatch(filename, g["file_glob"])]
    if len(hits) > 1:
        raise BuildError(
            f"{filename}: matches multiple grid bindings "
            f"{[g['name'] for g in hits]}; bindings must be unambiguous"
        )
    if hits:
        return hits[0], None
    for u in unbound:
        if fnmatch.fnmatch(filename, u["file_glob"]):
            return None, u["reason"]
    return None, None


def _is_placeholder_record(r) -> bool:
    """True for sentinel parm rows that are not real products.

    AQM parm files start with a 'SUPER WMO HEADER' row whose PDT is entirely
    wildcards -- a routing sentinel, not a product. Such a record has no usable
    parameter identity, so it must not enter the registry.
    """
    desc = (r.desc or "").strip().upper()
    if desc == "SUPER WMO HEADER":
        return True
    # A record with no non-wildcard PDT value has no identity to match on.
    fields = r.pdt_fields()
    if fields and all(v is None for v in fields.values()):
        return True
    return False


def build_tocgrib2_source(src: dict, cache_dir: Path) -> dict:
    """Build registry entries for one tocgrib2_parm source."""
    print(f"Building source {src['id']!r} @ {src['tag']}")
    tarball = fetch_tarball(src["tarball"], cache_dir)

    # The tag alone is not quite enough: git tags can be moved. Pinning the
    # tarball hash makes sources.json a complete audit trail on its own, which
    # matters because the generated manifests are not committed.
    observed = sha256_bytes(tarball.read_bytes())
    expected = src.get("expected_sha256")
    if expected and expected != "PIN_AFTER_FIRST_BUILD" and expected != observed:
        raise BuildError(
            f"{src['id']}: tarball sha256 mismatch -- the tag may have moved.\n"
            f"  expected {expected}\n  observed {observed}\n"
            f"Review the source at {src['tag']}, then update expected_sha256."
        )
    print(f"  tarball sha256: {observed}")

    files = read_parm_files(tarball, src["parm_dir"])
    if not files:
        raise BuildError(f"{src['id']}: no files found under {src['parm_dir']}")
    print(f"  parm files: {len(files)}")

    binding = src["grid_binding"]
    grids = binding["grids"]
    unbound = binding.get("unbound", [])

    entries: list = []
    file_manifest: list = []
    unmapped: list = []
    skipped_unbound = 0

    for name in sorted(files):
        text = files[name]
        grid, unbound_reason = match_grid(name, grids, unbound)

        if grid is None and unbound_reason is None:
            unmapped.append(name)
            continue

        records = parse_text(text, path=name)

        # Integrity: every GRIBIDS line must have parsed.
        raw_count = sum(1 for ln in text.splitlines() if "GRIBIDS" in ln.upper())
        if raw_count != len(records):
            raise BuildError(
                f"{name}: {raw_count} GRIBIDS lines but {len(records)} parsed; "
                f"refusing to build a partial registry"
            )

        # Integrity: PDT must be interpretable.
        problems = validate(records)
        if problems:
            raise BuildError(
                f"{name}: {len(problems)} uninterpretable record(s); first: "
                f"{problems[0]}"
            )

        file_manifest.append(
            {
                "file": name,
                "sha256": sha256_bytes(text.encode()),
                "records": len(records),
                "grid": grid["name"] if grid else None,
                "usable": grid is not None,
                "unusable_reason": unbound_reason,
            }
        )

        if grid is None:
            skipped_unbound += len(records)
            continue

        # Drop non-product placeholder records. Some parm sets (e.g. AQM) begin
        # each file with a 'SUPER WMO HEADER' sentinel whose PDT is all wildcards;
        # it is not a real product and must not enter the registry.
        records = [r for r in records if not _is_placeholder_record(r)]

        # Integrity: A1 must match the independently-published grid designator --
        # but only where the binding declares one. Some sources (e.g. AQM) encode
        # something other than grid in A1, so a single expected A1 per file does
        # not apply; those bindings omit 'expect_a1' and this check is skipped.
        expect_a1 = grid.get("expect_a1")
        if expect_a1 is not None:
            bad = {r.ttaaii[2] for r in records} - {expect_a1}
            if bad:
                raise BuildError(
                    f"{name}: grid binding {grid['name']!r} expects A1="
                    f"{expect_a1!r} (ON-388 Table A.2: {grid.get('a1_meaning_on388')}) "
                    f"but found A1 {sorted(bad)}; grid binding may have changed"
                )

        # Model cycle, when the parm filename encodes it (e.g. '...t06z...').
        # Authoritative: it comes from the filename, not the header letters. Used
        # at resolve time to disambiguate otherwise-identical records that differ
        # only by cycle. None when the filename carries no cycle (GFS/GEFS).
        cyc_m = re.search(r"t(\d{2})z", name)
        cycle = int(cyc_m.group(1)) if cyc_m else None

        for r in records:
            entries.append(
                {
                    "ttaaii": r.ttaaii,
                    "cccc": r.cccc,
                    "grid": grid["name"],
                    "desc": r.desc.strip(),
                    "pdtn": r.pdtn,
                    "pdt": r.pdt_fields(),
                    "cycle": cycle,
                    "source_id": src["id"],
                    "source_file": name,
                    "source_line": r.line_no,
                    "source_raw": r.raw,
                }
            )

    if unmapped:
        raise BuildError(
            f"{src['id']}: {len(unmapped)} parm file(s) match no grid binding "
            f"and no documented-unbound rule: {unmapped[:5]}. Add them to "
            f"sources.json (bound or unbound) so coverage stays explicit."
        )

    print(f"  usable records : {len(entries)}")
    print(f"  unbound records: {skipped_unbound} (recorded, not matchable)")

    return {
        "source": {
            "id": src["id"],
            "kind": src["kind"],
            "authority": src["authority"],
            "repo": src["repo"],
            "tag": src["tag"],
            "tarball": src["tarball"],
            "tarball_sha256": observed,
            "parm_dir": src["parm_dir"],
            "tocgrib2_reference": src["tocgrib2_reference"],
            "grid_binding": binding,
        },
        "files": file_manifest,
        "entries": entries,
    }


def build_tocgrib2_url_source(src: dict, cache_dir: Path) -> dict:
    """Build a tocgrib2_parm source whose parm files are loose URLs, not a tarball.

    Same record format and integrity checks as :func:`build_tocgrib2_source`, but
    the parm files are fetched individually from the NCO ``nwprod`` server (which
    publishes them as loose files per model version) rather than extracted from a
    GitHub release tarball. Each file is pinned by sha256 in ``sources.json``.
    """
    print(f"Building source {src['id']!r} @ {src['version']}")
    binding = src["grid_binding"]
    grids = binding["grids"]
    unbound = binding.get("unbound", [])

    entries: list = []
    file_manifest: list = []
    files = src["parm_files"]  # list of {url, expected_sha256}

    for spec in files:
        url = spec["url"]
        name = url.rsplit("/", 1)[-1]
        data = fetch_bytes(url, cache_dir)
        observed = sha256_bytes(data)
        expected = spec.get("expected_sha256")
        if expected and expected not in ("PIN_AFTER_FIRST_BUILD", observed):
            raise BuildError(
                f"{name}: sha256 mismatch -- source changed.\n"
                f"  expected {expected}\n  observed {observed}"
            )
        text = data.decode("utf-8", errors="replace")

        grid, unbound_reason = match_grid(name, grids, unbound)
        if grid is None and unbound_reason is None:
            raise BuildError(
                f"{src['id']}: parm file {name!r} matches no grid binding; add it "
                f"to sources.json (bound or unbound)."
            )

        records = parse_text(text, path=name)
        raw_count = sum(1 for ln in text.splitlines() if "GRIBIDS" in ln.upper())
        if raw_count != len(records):
            raise BuildError(
                f"{name}: {raw_count} GRIBIDS lines but {len(records)} parsed; "
                f"refusing to build a partial registry"
            )
        problems = validate(records)
        if problems:
            raise BuildError(
                f"{name}: {len(problems)} uninterpretable record(s); first: "
                f"{problems[0]}"
            )

        file_manifest.append({
            "file": name,
            "url": url,
            "sha256": observed,
            "records": len(records),
            "grid": grid["name"] if grid else None,
            "usable": grid is not None,
            "unusable_reason": unbound_reason,
        })
        if grid is None:
            continue

        records = [r for r in records if not _is_placeholder_record(r)]

        expect_a1 = grid.get("expect_a1")
        if expect_a1 is not None:
            bad = {r.ttaaii[2] for r in records} - {expect_a1}
            if bad:
                raise BuildError(
                    f"{name}: grid binding {grid['name']!r} expects A1="
                    f"{expect_a1!r} (ON-388 Table A.2: {grid.get('a1_meaning_on388')}) "
                    f"but found A1 {sorted(bad)}; grid binding may have changed"
                )

        for r in records:
            entries.append({
                "ttaaii": r.ttaaii,
                "cccc": r.cccc,
                "grid": grid["name"],
                "desc": r.desc.strip(),
                "pdtn": r.pdtn,
                "pdt": r.pdt_fields(),
                "cycle": None,
                "source_id": src["id"],
                "source_file": name,
                "source_line": r.line_no,
                "source_raw": r.raw,
            })

    print(f"  parm files     : {len(file_manifest)}")
    print(f"  usable records : {len(entries)}")

    return {
        "source": {
            "id": src["id"],
            # Emit the canonical kind so the Registry indexes these records in
            # by_grid alongside the tarball-sourced tocgrib2 parm records.
            "kind": "tocgrib2_parm",
            "authority": src["authority"],
            "version": src["version"],
            "tocgrib2_reference": src["tocgrib2_reference"],
            "grid_binding": binding,
        },
        "files": file_manifest,
        "entries": entries,
    }


def fetch_bytes(url: str, cache_dir: Path) -> bytes:
    """Download a document (cached by URL hash)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    dest = cache_dir / (sha256_bytes(url.encode())[:16] + ".doc")
    if dest.exists():
        return dest.read_bytes()
    print(f"  fetching: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "wmo-registry-build"})
    with urllib.request.urlopen(req) as resp:
        data = resp.read()
    dest.write_bytes(data)
    return data


def pdf_to_text(data: bytes) -> str:
    reader = pypdf.PdfReader(io.BytesIO(data))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


# table_4_2_<discipline>_<category> = { "<number>": [name, units, abbrev], ... }
_PARAM_TABLE_RE = re.compile(
    r"^table_4_2_(\d+)_(\d+)\s*=\s*(\{.*?\n\})", re.DOTALL | re.MULTILINE
)


def _fetch_text_lines(url: str, cache_dir: Path):
    """Fetch a document and return (raw_bytes, list of CR/LF-stripped lines)."""
    data = fetch_bytes(url, cache_dir)
    text = data.decode("utf-8", errors="replace")
    lines = [ln.rstrip("\r\n") for ln in text.splitlines()]
    return data, lines


def _norm_ws(s: str) -> str:
    return " ".join(s.split())


def build_nws_analysis_header_source(src: dict, cache_dir: Path) -> dict:
    """Build the RTMA/URMA analysis-header tables from a curated, TIN-cited file.

    The RTMA/URMA header is compositional: ``T1 T2 A1 A2ii`` with T1='L',
    A2ii='A98', CCCC='KWBR', T2 from the parameter and A1 from the
    domain/resolution. The '*' legend that defines A1 is prose in the TINs, so
    the mapping is human-curated in ``registry/<curated_file>`` -- but every
    entry cites a verbatim TIN quote, the TIN PDFs are pinned by sha256, and
    this builder verifies each quote actually appears in its TIN. So the
    curation is checkable, not trusted.
    """
    print(f"Building source {src['id']!r}")
    curated_path = HERE / "registry" / src["curated_file"]
    cur = json.loads(curated_path.read_text())

    # Fetch + verify each TIN, and extract its text for quote validation.
    doc_text: dict = {}
    doc_manifest: list = []
    for doc in cur["documents"]:
        data = fetch_bytes(doc["url"], cache_dir)
        observed = sha256_bytes(data)
        expected = doc.get("expected_sha256")
        if expected and expected != "PIN_AFTER_FIRST_BUILD" and expected != observed:
            raise BuildError(
                f"{src['id']}/{doc['id']}: sha256 mismatch.\n"
                f"  expected {expected}\n  observed {observed}"
            )
        text = _norm_ws(pdf_to_text(data))
        doc_text[doc["id"]] = text
        doc_manifest.append(
            {"id": doc["id"], "title": doc["title"], "url": doc["url"],
             "sha256": observed}
        )

    def verify(entry, kind):
        did = entry["source"]
        if did not in doc_text:
            raise BuildError(f"{src['id']}: entry cites unknown document {did!r}")
        if _norm_ws(entry["quote"]) not in doc_text[did]:
            raise BuildError(
                f"{src['id']}: {kind} entry for "
                f"{entry.get('short_name') or entry.get('domain')!r} cites a quote "
                f"not found in {did}: {entry['quote']!r}"
            )

    param_t2: dict = {}
    for e in cur["parameter_t2"]:
        verify(e, "parameter_t2")
        param_t2[normalize_name(e["short_name"])] = {
            "t2": e["t2"], "desc": e["desc"], "source": e["source"]
        }

    domain_a1: dict = {}
    for e in cur["domain_a1"]:
        verify(e, "domain_a1")
        key = f"{e['dataset']}|{e['resolution']}|{e['domain']}"
        domain_a1[key] = {"a1": e["a1"], "source": e["source"]}

    print(f"  documents verified: {len(doc_manifest)}")
    print(f"  parameter->T2: {len(param_t2)}  domain->A1: {len(domain_a1)}")

    return {
        "source": {
            "id": src["id"], "kind": src["kind"], "authority": src["authority"],
            "cccc": cur["cccc"], "t1": cur["t1"], "a2ii": cur["a2ii"],
            "curated_file": src["curated_file"],
        },
        "files": doc_manifest,
        "analysis_param_t2": param_t2,
        "analysis_domain_a1": domain_a1,
        "entries": [],
    }


def build_cccc_directory_source(src: dict, cache_dir: Path) -> dict:
    """Build the set of valid CCCC office codes from XR-09 (CCC<TAB>KCCCC)."""
    print(f"Building source {src['id']!r}")
    data, lines = _fetch_text_lines(src["url"], cache_dir)

    observed = sha256_bytes(data)
    expected = src.get("expected_sha256")
    if expected and expected != "PIN_AFTER_FIRST_BUILD" and expected != observed:
        raise BuildError(
            f"{src['id']}: sha256 mismatch.\n"
            f"  expected {expected}\n  observed {observed}"
        )

    # rows: CCC<TAB>KCCCC. One CCC node serves many offices, so the relation is
    # one-to-many (CCC -> list of CCCC); the primary product is the set of valid
    # CCCC office codes.
    cccc_to_ccc: dict = {}
    n_rows = 0
    row_re = re.compile(r"^([A-Z0-9]{3})\t([A-Z]{4})\s*$")
    for ln in lines:
        m = row_re.match(ln)
        if not m:
            continue
        ccc, cccc = m.group(1), m.group(2)
        if ccc == "CCC":  # header row 'CCC MAR'
            continue
        cccc_to_ccc[cccc] = ccc  # KCCCC is unique; CCC is its node
        n_rows += 1

    if not cccc_to_ccc:
        raise BuildError(f"{src['id']}: no CCC->CCCC rows parsed; format changed?")

    cccc_set = sorted(cccc_to_ccc)
    print(f"  rows: {n_rows}  distinct CCCC offices: {len(cccc_set)}")

    return {
        "source": {
            "id": src["id"], "kind": src["kind"], "authority": src["authority"],
            "url": src["url"], "note": src.get("note", ""), "sha256": observed,
        },
        "files": [{"url": src["url"], "sha256": observed, "rows": n_rows}],
        "cccc_to_ccc": cccc_to_ccc,
        "valid_cccc": cccc_set,
        "entries": [],
    }


def build_awips_xref_source(src: dict, cache_dir: Path) -> dict:
    """Build the AWIPS NNN -> (TT list, ii, description) table from XR-04."""
    print(f"Building source {src['id']!r}")
    data, lines = _fetch_text_lines(src["url"], cache_dir)

    observed = sha256_bytes(data)
    expected = src.get("expected_sha256")
    if expected and expected != "PIN_AFTER_FIRST_BUILD" and expected != observed:
        raise BuildError(
            f"{src['id']}: sha256 mismatch.\n"
            f"  expected {expected}\n  observed {observed}"
        )

    # rows: NNN<TAB>TT<TAB>ii<TAB>DESCRIPTION. NNN is 3 alphanumerics; TT may be
    # multi-valued ('NO/WO'); ii may be a placeholder ('4i','ce','8i/ce/it').
    xref: dict = {}
    row_re = re.compile(r"^([A-Z0-9]{3})\t([A-Z0-9/]+)\t(\S+)\t(.+?)\s*$")
    for ln in lines:
        m = row_re.match(ln)
        if not m:
            continue
        nnn, tt, ii, desc = m.groups()
        if nnn == "NNN":  # header row
            continue
        xref[nnn] = {
            "tt": tt.split("/"),          # e.g. ['NO','WO']
            "ii": ii,                     # kept verbatim incl. placeholders
            "description": desc.strip(),
        }

    if not xref:
        raise BuildError(f"{src['id']}: no NNN rows parsed; format changed?")

    print(f"  AWIPS NNN categories: {len(xref)}")

    return {
        "source": {
            "id": src["id"], "kind": src["kind"], "authority": src["authority"],
            "url": src["url"], "note": src.get("note", ""), "sha256": observed,
        },
        "files": [{"url": src["url"], "sha256": observed, "rows": len(xref)}],
        "awips_xref": xref,
        "entries": [],
    }


def build_grib2_param_table_source(src: dict, cache_dir: Path) -> dict:
    """Build abbreviation -> (discipline, category, number) from grib2io tables.

    grib2io (NOAA-MDL) ships the authoritative GRIB2 parameter tables as Python
    dicts named ``table_4_2_<discipline>_<category>`` mapping a parameter number
    to ``[long_name, units, abbreviation]``. We invert them to a mapping from the
    NCEP abbreviation (the ``short_name`` archives carry) to its GRIB2 identity.

    This is only a parameter-identity source: it says what a field *is*, not
    whether it has a WMO header. It closes the "abbreviation unknown" gap and
    makes the parameter step independently sourced rather than derived from the
    parm files themselves.
    """
    print(f"Building source {src['id']!r} @ {src['tag']}")
    tarball = fetch_tarball(src["tarball"], cache_dir)

    observed = sha256_bytes(tarball.read_bytes())
    expected = src.get("expected_sha256")
    if expected and expected != "PIN_AFTER_FIRST_BUILD" and expected != observed:
        raise BuildError(
            f"{src['id']}: tarball sha256 mismatch.\n"
            f"  expected {expected}\n  observed {observed}"
        )
    print(f"  tarball sha256: {observed}")

    table_dir = src["table_dir"].rstrip("/") + "/"
    file_manifest: list = []
    # abbrev -> set of (discipline, category, number)
    abbrev_ids: dict = {}
    n_params = 0

    with tarfile.open(tarball, "r:gz") as tf:
        members = [
            m for m in tf.getmembers()
            if m.isfile()
            and table_dir in m.name
            and re.search(r"section4_discipline\d+\.py$", m.name)
        ]
        if not members:
            raise BuildError(
                f"{src['id']}: no section4_discipline*.py under {table_dir}"
            )
        for m in sorted(members, key=lambda x: x.name):
            text = tf.extractfile(m).read().decode("utf-8", errors="replace")
            fname = m.name.split("/")[-1]
            file_params = 0
            for tm in _PARAM_TABLE_RE.finditer(text):
                discipline = int(tm.group(1))
                category = int(tm.group(2))
                try:
                    table = ast.literal_eval(tm.group(3))
                except (ValueError, SyntaxError) as e:
                    raise BuildError(
                        f"{src['id']}: could not parse table_4_2_"
                        f"{discipline}_{category} in {fname}: {e}"
                    )
                for num_s, row in table.items():
                    if not isinstance(row, list) or len(row) < 3:
                        continue
                    abbrev = str(row[2]).strip()
                    if not abbrev or abbrev.lower() in ("unknown", "reserved", "missing"):
                        continue
                    try:
                        number = int(num_s)
                    except ValueError:
                        continue
                    key = normalize_name(abbrev)
                    abbrev_ids.setdefault(key, set()).add(
                        (discipline, category, number)
                    )
                    file_params += 1
                    n_params += 1
            file_manifest.append(
                {"file": fname, "sha256": sha256_bytes(text.encode()),
                 "parameters": file_params}
            )
            print(f"  {fname:34s} params={file_params}")

    # Split into unambiguous and ambiguous abbreviations.
    params: dict = {}
    ambiguous: dict = {}
    for abbrev, ids in abbrev_ids.items():
        if len(ids) == 1:
            d, c, n = next(iter(ids))
            params[abbrev] = {"discipline": d, "category": c, "number": n}
        else:
            ambiguous[abbrev] = sorted([list(x) for x in ids])

    print(f"  abbreviations  : {len(params)} unique, {len(ambiguous)} ambiguous")

    return {
        "source": {
            "id": src["id"],
            "kind": src["kind"],
            "authority": src["authority"],
            "repo": src["repo"],
            "tag": src["tag"],
            "tarball": src["tarball"],
            "tarball_sha256": observed,
            "table_dir": src["table_dir"],
        },
        "files": file_manifest,
        "param_abbrev": params,
        "param_abbrev_ambiguous": ambiguous,
        "entries": [],
    }


def build_wmo_code_table_source(src: dict, cache_dir: Path) -> dict:
    """Build code-value lookups from the WMO machine-readable GRIB2 CSV tables."""
    print(f"Building source {src['id']!r}")
    cols = src["csv_columns"]
    tables: dict = {}
    file_manifest: list = []

    for tbl in src["tables"]:
        data = fetch_bytes(tbl["url"], cache_dir)
        observed = sha256_bytes(data)
        expected = tbl.get("expected_sha256")
        if expected and expected != "PIN_AFTER_FIRST_BUILD" and expected != observed:
            raise BuildError(
                f"table {tbl['id']}: sha256 mismatch -- source table changed.\n"
                f"  expected {expected}\n  observed {observed}"
            )

        reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig", errors="replace")))
        for required in (cols["code"], cols["meaning"]):
            if required not in (reader.fieldnames or []):
                raise BuildError(
                    f"table {tbl['id']}: expected column {required!r}, "
                    f"got {reader.fieldnames}"
                )

        rows = []
        for row in reader:
            code_s = (row.get(cols["code"]) or "").strip()
            meaning = (row.get(cols["meaning"]) or "").strip()
            if not code_s.isdigit() or not meaning:
                continue
            if meaning.lower() in ("reserved", "missing"):
                continue
            rows.append(
                {
                    "code": int(code_s),
                    "meaning": meaning,
                    "status": (row.get(cols["status"]) or "").strip(),
                }
            )
        if not rows:
            raise BuildError(f"table {tbl['id']}: no usable rows parsed")

        tables[tbl["id"]] = {"name": tbl["name"], "rows": rows}
        file_manifest.append(
            {
                "id": tbl["id"],
                "name": tbl["name"],
                "url": tbl["url"],
                "sha256": observed,
                "rows": len(rows),
            }
        )
        print(f"  table {tbl['id']:5s} {tbl['name']:26s} rows={len(rows)}")

    return {
        "source": {
            "id": src["id"],
            "kind": src["kind"],
            "authority": src["authority"],
            "repo": src["repo"],
            "ref": src["ref"],
            "note": src.get("note", ""),
        },
        "files": file_manifest,
        "code_tables": tables,
        "entries": [],
    }


def _canonical_entries(entries: list) -> str:
    """Stable serialization of the assignments extracted from a document.

    Used as the integrity hash for sources whose raw bytes are not stable. Only
    the fields that can affect a resolved header are included.
    """
    rows = sorted(
        (
            e.code,
            e.domain,
            e.status or "",
            ";".join(sorted(e.product_names)),
            "1" if e.ambiguous else "0",
            ";".join(sorted(e.candidates)),
        )
        for e in entries
    )
    return "\n".join("|".join(r) for r in rows)


def build_nws_notice_source(src: dict, cache_dir: Path) -> dict:
    """Build registry entries for one nws_notice source."""
    print(f"Building source {src['id']!r}")

    all_entries: list = []
    doc_manifest: list = []
    cccc = None

    for doc in src["documents"]:
        data = fetch_bytes(doc["url"], cache_dir)

        if doc["format"] == "pdf":
            text = pdf_to_text(data)
        else:
            text = data.decode("utf-8", errors="replace")

        entries: list = []
        if "A" in doc["layouts"]:
            entries += parse_notice(text, doc["id"])
        if "B" in doc["layouts"]:
            entries += parse_html_product_annotations(text, doc["id"])

        if not entries:
            raise BuildError(
                f"{doc['id']}: no header entries extracted; the document layout "
                f"may have changed"
            )

        # Integrity hash. 'bytes' suits stable documents (PDFs). Live HTML pages
        # embed rotating content, so hashing raw bytes there would fail on
        # changes that have nothing to do with the header assignments; 'extracted'
        # instead hashes the assignments we actually depend on, which still
        # detects any change that could alter a resolved header.
        mode = doc.get("hash_mode", "bytes")
        if mode == "bytes":
            observed = sha256_bytes(data)
        elif mode == "extracted":
            observed = sha256_bytes(_canonical_entries(entries).encode())
        else:
            raise BuildError(f"{doc['id']}: unknown hash_mode {mode!r}")

        expected = doc.get("expected_sha256")
        if expected and expected != "PIN_AFTER_FIRST_BUILD" and expected != observed:
            raise BuildError(
                f"{doc['id']}: sha256 mismatch ({mode}) -- source changed.\n"
                f"  expected {expected}\n  observed {observed}\n"
                f"Review the source, then update expected_sha256."
            )

        if doc["id"] == src.get("cccc_declared_by"):
            cccc = extract_cccc(text)
            if not cccc:
                raise BuildError(
                    f"{doc['id']}: expected an explicit 'CCCC = XXXX' declaration "
                    f"but found none"
                )

        doc_manifest.append(
            {
                "id": doc["id"],
                "title": doc["title"],
                "url": doc["url"],
                "format": doc["format"],
                "layouts": doc["layouts"],
                "role": doc.get("role", ""),
                "hash_mode": mode,
                "sha256": observed,
                "entries": len(entries),
            }
        )
        all_entries += entries

    if not cccc:
        raise BuildError(
            f"{src['id']}: CCCC was never determined; "
            f"cccc_declared_by={src.get('cccc_declared_by')!r}"
        )

    # Flag codes associated with more than one product; these become unusable.
    conflicts = find_conflicts(all_entries)
    mark_conflicts(all_entries)

    contradictions = {
        k: v for k, v in conflicts.items() if v["kind"] == "contradiction"
    }
    variants = {k: v for k, v in conflicts.items() if v["kind"] == "variant_ambiguity"}

    usable = [e for e in all_entries if not e.ambiguous and e.product_names]
    print(f"  documents        : {len(doc_manifest)}")
    print(f"  entries          : {len(all_entries)}")
    print(f"  name-matchable   : {len(usable)}")
    print(f"  contradictions   : {len(contradictions)} (source disagrees with itself)")
    print(f"  variant ambiguity: {len(variants)} (code covers unstated variants)")
    for k, v in sorted(contradictions.items()):
        print(f"    ! {v['reason']}")

    out_entries = [
        {
            "code": e.code,
            "cccc": cccc,
            "domain": e.domain,
            "a1": e.a1,
            "description": e.description,
            "status": e.status,
            "layout": e.layout,
            "product_names": e.product_names,
            "usable_for_matching": bool(e.product_names) and not e.ambiguous,
            "ambiguous": e.ambiguous,
            "ambiguity_reason": e.ambiguity_reason,
            "candidates": e.candidates,
            "annotation_qualifier": e.annotation_qualifier,
            "source_id": src["id"],
            "source_doc": e.doc_id,
            "source_raw": e.raw,
        }
        for e in all_entries
    ]

    return {
        "source": {
            "id": src["id"],
            "kind": src["kind"],
            "authority": src["authority"],
            "matching": src.get("matching", ""),
            "domain_from": src.get("domain_from", ""),
            "cccc": cccc,
            "cccc_declared_by": src.get("cccc_declared_by"),
            "known_source_defects": src.get("known_source_defects", []),
        },
        "files": doc_manifest,
        "conflicts": {
            f"{k[0]}/{k[1]}": {"kind": v["kind"], "reason": v["reason"]}
            for k, v in conflicts.items()
        },
        "entries": out_entries,
    }


def build_manifest(built: dict) -> dict:
    """Assemble a source's manifest dict (no I/O).

    The manifest holds everything about a source except its per-entry rows:
    provenance (``source``, ``files``), the entry count, and the per-source
    lookup data that lives outside the entries (code tables, grid specs, CCCC
    lists, abbreviation maps, analysis-header tables). The entries themselves are
    carried alongside in ``built["entries"]`` and stored separately.
    """
    manifest = {
        "source": built["source"],
        "files": built["files"],
        "entry_count": len(built["entries"]),
    }
    if "conflicts" in built:
        manifest["conflicts"] = built["conflicts"]
    if "code_tables" in built:
        manifest["code_tables"] = built["code_tables"]
    if "param_abbrev" in built:
        manifest["param_abbrev"] = built["param_abbrev"]
        manifest["param_abbrev_ambiguous"] = built["param_abbrev_ambiguous"]
    if "valid_cccc" in built:
        manifest["cccc_to_ccc"] = built["cccc_to_ccc"]
        manifest["valid_cccc"] = built["valid_cccc"]
    if "awips_xref" in built:
        manifest["awips_xref"] = built["awips_xref"]
    if "analysis_param_t2" in built:
        manifest["analysis_param_t2"] = built["analysis_param_t2"]
        manifest["analysis_domain_a1"] = built["analysis_domain_a1"]
        manifest["cccc"] = built["source"]["cccc"]
        manifest["t1"] = built["source"]["t1"]
        manifest["a2ii"] = built["source"]["a2ii"]
    return manifest


def build_nexrad_radar_source(src: dict, cache_dir: Path) -> dict:
    """Build registry entries from the NOAAPort Radar Products table.

    One entry per (product_code, NNN) row. CCCC is per-site at dissemination and
    is not in the table, so it is left to the resolver (taken from the store's
    site_id); the table pins the TTAAii prefix (SDUS<tier>) and NNN.
    """
    from nexrad_radar import parse_noaaport_radar

    print(f"Building source {src['id']!r}")
    doc = src["documents"][0]
    data = fetch_bytes(doc["url"], cache_dir)
    observed = sha256_bytes(data)
    expected = doc.get("expected_sha256")
    if expected and expected not in ("PIN_AFTER_FIRST_BUILD", observed):
        raise BuildError(
            f"{doc['id']}: sha256 mismatch -- source changed.\n"
            f"  expected {expected}\n  observed {observed}"
        )

    text = pdf_to_text(data)
    parsed = parse_noaaport_radar(text)
    if not parsed:
        raise BuildError(
            f"{doc['id']}: no radar product rows extracted; table layout may "
            f"have changed"
        )

    out_entries = [
        {
            "product_code": e.product_code,
            "rpg_header": e.rpg_header,
            "nnn": e.nnn,
            "t1t2": e.t1t2,
            "tier": e.tier,
            "ttaaii_prefix": e.ttaaii_prefix,
            "elevation": e.elevation,
            "elevation_values": e.elevation_values,
            "radar_kind": e.radar_kind,
            "source_id": src["id"],
            "source_doc": doc["id"],
            "source_raw": e.raw,
        }
        for e in parsed
    ]

    codes = sorted({e["product_code"] for e in out_entries})
    print(f"  rows            : {len(out_entries)}")
    print(f"  product codes   : {len(codes)}")

    return {
        "source": {
            "id": src["id"],
            "kind": src["kind"],
            "authority": src["authority"],
            "matching": src.get("matching", ""),
        },
        "files": [
            {
                "id": doc["id"],
                "title": doc["title"],
                "url": doc["url"],
                "format": doc["format"],
                "sha256": observed,
                "entries": len(out_entries),
            }
        ],
        "entries": out_entries,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", default=None, help="Build only this source id.")
    ap.add_argument("--cache-dir", default="/tmp/wmo_registry_src", type=Path)
    ap.add_argument("--out-dir", default=REGISTRY_DIR, type=Path,
                    help="Registry directory; the database is written to "
                         "<out-dir>/registry.db")
    args = ap.parse_args(argv)

    db_path = Path(args.out_dir) / REGISTRY_DB.name

    manifest = json.loads(SOURCES_JSON.read_text())
    sources = manifest["sources"]
    single = bool(args.source)
    if single:
        sources = [s for s in sources if s["id"] == args.source]
        if not sources:
            print(f"No such source: {args.source}", file=sys.stderr)
            return 2

    builders = {
        "tocgrib2_parm": build_tocgrib2_source,
        "nws_notice": build_nws_notice_source,
        "wmo_code_table": build_wmo_code_table_source,
        "grib2_param_table": build_grib2_param_table_source,
        "cccc_directory": build_cccc_directory_source,
        "nws_awips_xref": build_awips_xref_source,
        "nws_analysis_header": build_nws_analysis_header_source,
        "nexrad_radar": build_nexrad_radar_source,
        "tocgrib2_parm_url": build_tocgrib2_url_source,
    }

    built_all: list = []
    for src in sources:
        builder = builders.get(src["kind"])
        if builder is None:
            print(f"Skipping {src['id']}: unknown kind {src['kind']!r}")
            continue
        try:
            built = builder(src, args.cache_dir)
        except BuildError as e:
            print(f"BUILD FAILED for {src['id']}: {e}", file=sys.stderr)
            return 1
        built_all.append({"manifest": build_manifest(built), "entries": built["entries"]})

    # A full build rewrites the database from scratch; a single-source build
    # upserts just that source so the others are left intact.
    if single:
        for b in built_all:
            registry_db.upsert_source(db_path, b)
            print(f"  upserted {b['manifest']['source']['id']} "
                  f"({b['manifest']['entry_count']} entries)")
    else:
        registry_db.write_db(db_path, built_all)
        total = sum(b["manifest"]["entry_count"] for b in built_all)
        print(f"Wrote {db_path.name}: {len(built_all)} sources, {total} entries")

    return 0


if __name__ == "__main__":
    sys.exit(main())
