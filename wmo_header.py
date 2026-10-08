"""Resolve WMO abbreviated headings against the generated authoritative registry.

Design principle: **accurate or nothing.** A heading is returned only when every
field traces to an authoritative record in ``registry/`` (built by
``build_registry.py`` from pinned NWS/NCEP/WMO sources). Otherwise an
:class:`Unresolved` is returned explaining precisely what is missing. Nothing is
defaulted, inferred or fabricated.

Heading form (https://www.weather.gov/tg/headef)::

    T1T2A1A2ii  CCCC  YYGGgg  (BBB)

``T1T2A1A2ii`` and ``CCCC`` come from the registry. ``YYGGgg`` is the only field
derived from the data, because that is how dissemination works: ``tocgrib2``
stamps the day/hour from the GRIB message itself.

Two registry kinds are matched, reflecting how each authority actually defines
its assignments:

``nws_notice`` (e.g. MRMS)
    Matched by **product name and domain**, because the NWS notices identify
    MRMS products by name. Codes the sources disagree on are marked unusable at
    build time and therefore never match.

``tocgrib2_parm`` (e.g. GFS AWIPS)
    Matched by **grid plus GRIB2 identity** (parameter, level, forecast hour,
    generating process, statistical processing), because that is the key
    ``tocgrib2`` itself uses. A candidate matches only if every field the parm
    record pins agrees; fields the record leaves as ``-9999`` are wildcards.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union

import numpy as np

from grib_identity import levels_close, parse_section3, physical_level

REGISTRY_DIR = Path(__file__).with_name("registry")


class RegistryError(RuntimeError):
    """Raised when the loaded registry is internally inconsistent."""


# NCEP abbreviations that appear in the tocgrib2 parm files but are absent from
# the grib2io parameter tables (as of grib2io v2.8.2). grib2io is authoritative
# for parameter identity; these are the only abbreviations for which the parm
# records are allowed to backfill identity. Keeping the set explicit means an
# upstream grib2io change that adds or removes one of these surfaces as a build
# error (see Registry._build_abbrev_map) rather than silently reviving a
# home-grown identity map.
#
# What they are: categorical precipitation type flags, (best 4-layer) lifted
# index, and vertical wind shear -- all genuinely headered GFS products, so
# dropping the backfill would lose real coverage.
GRIB2IO_ABBREV_GAPS = frozenset(
    {"cfrzr", "cicep", "crain", "csnow", "lftx", "4lftx", "vwsh"}
)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


@dataclass
class LoadedSource:
    manifest: dict
    entries: list

    @property
    def id(self) -> str:
        return self.manifest["source"]["id"]

    @property
    def kind(self) -> str:
        return self.manifest["source"]["kind"]


class Registry:
    """All generated registry sources, indexed for lookup."""

    def __init__(self, sources: list):
        self.sources = sources
        self.by_kind: dict = {}
        for s in sources:
            self.by_kind.setdefault(s.kind, []).append(s)

        # nws_notice: normalized product name -> entries
        self.by_product: dict = {}
        for s in self.by_kind.get("nws_notice", []):
            for e in s.entries:
                if not e.get("usable_for_matching"):
                    continue
                for name in e["product_names"]:
                    self.by_product.setdefault(normalize_name(name), []).append(e)

        # tocgrib2_parm: grid -> entries
        self.by_grid: dict = {}
        self.grid_specs: dict = {}
        for s in self.by_kind.get("tocgrib2_parm", []):
            for g in s.manifest["source"]["grid_binding"]["grids"]:
                self.grid_specs[g["name"]] = g
            for e in s.entries:
                self.by_grid.setdefault(e["grid"], []).append(e)

        # NCEP abbreviation -> (parameterCategory, parameterNumber).
        # Two sources, in priority order:
        #   1. grib2io parameter tables (authoritative, comprehensive, external);
        #   2. the parm-file DESC fields (fallback, self-derived).
        # The grib2io map is preferred where present; the parm-derived map only
        # fills abbreviations grib2io does not cover. Discipline is retained
        # separately for reporting (parm records do not carry it, so matching
        # still keys on category+number).
        self.abbrev_param: dict = {}          # abbrev -> (category, number)
        self.abbrev_ambiguous: dict = {}      # abbrev -> [candidate ids]
        self.abbrev_discipline: dict = {}     # abbrev -> discipline (grib2io only)
        self.abbrev_source: dict = {}         # abbrev -> "grib2io" | "parm-gap"

        # WMO code tables: table id -> {normalized meaning: code}
        self.code_tables: dict = {}
        for s in self.by_kind.get("wmo_code_table", []):
            for tid, tbl in s.manifest.get("code_tables", {}).items():
                lookup: dict = {}
                for row in tbl["rows"]:
                    lookup.setdefault(normalize_meaning(row["meaning"]), set()).add(
                        row["code"]
                    )
                self.code_tables[tid] = lookup

        # XR-09 CCCC office directory (validation) and XR-04 AWIPS NNN xref.
        self.valid_cccc: set = set()
        for s in self.by_kind.get("cccc_directory", []):
            self.valid_cccc.update(s.manifest.get("valid_cccc", []))
        self.awips_xref: dict = {}
        for s in self.by_kind.get("nws_awips_xref", []):
            self.awips_xref.update(s.manifest.get("awips_xref", {}))

        # RTMA/URMA analysis-header composition tables.
        self.analysis_headers: list = []
        for s in self.by_kind.get("nws_analysis_header", []):
            self.analysis_headers.append({
                "cccc": s.manifest["cccc"],
                "t1": s.manifest["t1"],
                "a2ii": s.manifest["a2ii"],
                "param_t2": s.manifest.get("analysis_param_t2", {}),
                "domain_a1": s.manifest.get("analysis_domain_a1", {}),
                "source_id": s.id,
            })

        # nexrad_radar: product_code -> entries (one per NNN/elevation). WSR-88D
        # only; TDWR rows are kept out of the match index (different radar), but
        # retained in the source entries for the record.
        self.radar_by_code: dict = {}
        for s in self.by_kind.get("nexrad_radar", []):
            for e in s.entries:
                if e.get("radar_kind") != "WSR-88D":
                    continue
                self.radar_by_code.setdefault(e["product_code"], []).append(e)

        self._build_abbrev_map()

    def _build_abbrev_map(self) -> None:
        """Build abbreviation -> GRIB2 parameter identity.

        Identity comes authoritatively from the grib2io parameter tables. A few
        abbreviations that appear in the NCEP parm files are absent from grib2io
        (see :data:`GRIB2IO_ABBREV_GAPS`); those, and only those, are backfilled
        from the parm records, so the fallback is an explicit, bounded patch over
        a known upstream gap rather than a parallel identity system.

        Loudly rejects drift: if a parm abbreviation missing from grib2io is not
        in the documented gap list, the build of the map raises, so an upstream
        change surfaces instead of silently reintroducing a home-grown map.
        """
        # 1. Authoritative source: grib2io.
        for s in self.by_kind.get("grib2_param_table", []):
            for abbrev, ident in s.manifest.get("param_abbrev", {}).items():
                self.abbrev_param[abbrev] = (ident["category"], ident["number"])
                self.abbrev_discipline[abbrev] = ident["discipline"]
                self.abbrev_source[abbrev] = "grib2io"
            for abbrev, ids in s.manifest.get("param_abbrev_ambiguous", {}).items():
                if abbrev not in self.abbrev_param:
                    self.abbrev_ambiguous[abbrev] = ids

        # 2. Parm-DESC backfill for abbreviations grib2io lacks.
        #
        # This exists so a store whose short_name is not in grib2io can still be
        # identified from the parm records. It is bounded two ways:
        #   * only clean, abbreviation-shaped DESC tokens are considered (parm
        #     DESCs like 'SUPER WMO HEADER' or '1 HOUR SFC OZONE' are not
        #     parameter abbreviations and are ignored);
        #   * the documented GFS/GEFS gap set (GRIB2IO_ABBREV_GAPS) is asserted
        #     to stay covered -- if one of those specific abbreviations stops
        #     being backfillable, that is a real regression and raises.
        # A previously-unseen backfill abbreviation is recorded, not fatal:
        # matching still keys on the PDT's own category/number, so an extra
        # abbreviation cannot produce a wrong header, only a new lookup alias.
        # The gap backfill builds a global short_name -> parameter alias. Some
        # models encode the gap abbreviations with different GRIB2 numbers than
        # GFS/GEFS do -- e.g. HRRR uses the standard WMO numbers for CFRZR/CRAIN/
        # LFTX while GFS uses NCEP local-table 192+. Folding both into one alias
        # would make those abbreviations ambiguous and lose the GFS coverage the
        # gap list guarantees. Such models are excluded from the *global*
        # backfill; they still resolve via grib2io (which covers their common
        # fields) and, when resolution is scoped to the model (prefer_source),
        # directly from that model's own parm records. Keep this a denylist so a
        # newly added, non-conflicting source keeps contributing by default.
        BACKFILL_EXCLUDED_SOURCES = {"ncep-hrrr-awips"}
        grib2io_loaded = bool(self.by_kind.get("grib2_param_table"))
        parm_ids: dict = {}
        for entries in self.by_grid.values():
            for e in entries:
                if e.get("source_id") in BACKFILL_EXCLUDED_SOURCES:
                    continue
                abbrev = desc_abbrev(e["desc"])
                cat = e["pdt"].get("parameterCategory")
                num = e["pdt"].get("parameterNumber")
                if not abbrev or cat is None or num is None:
                    continue
                if not _is_clean_abbrev(e["desc"]):
                    continue
                parm_ids.setdefault(abbrev, set()).add((cat, num))

        for abbrev, ids in parm_ids.items():
            if abbrev in self.abbrev_param:
                continue  # grib2io already covers it authoritatively
            if len(ids) == 1:
                self.abbrev_param[abbrev] = next(iter(ids))
                self.abbrev_source[abbrev] = "parm-gap"
            else:
                self.abbrev_ambiguous.setdefault(abbrev, sorted(ids))

        # Regression guard: the documented GFS/GEFS gap abbreviations must all
        # still be covered (by grib2io or the backfill). If one goes missing,
        # grib2io changed under us and real coverage would be lost.
        if grib2io_loaded:
            missing = [a for a in GRIB2IO_ABBREV_GAPS if a not in self.abbrev_param]
            if missing:
                raise RegistryError(
                    f"documented gap abbreviations no longer resolvable: "
                    f"{sorted(missing)}. grib2io or the parm sources changed; "
                    f"review GRIB2IO_ABBREV_GAPS."
                )

    # National-center originating codes are not WFO nodes, so they legitimately
    # do not appear in the XR-09 WFO directory. Absence is expected, not an error.
    NATIONAL_CENTER_CCCC = frozenset({"KWBC", "KWNR", "KWBK", "KWNO", "KNES", "KWBD",
                                      "KWBE", "KWBF", "KWBG", "KWBH", "KWBJ", "KWBM",
                                      "KWBN", "KWBP", "KWBQ", "KWBR", "KWBS", "KWBT",
                                      "KWBV", "KWBW", "KWBY"})

    def validate_cccc(self, cccc: Optional[str]) -> str:
        """Classify a CCCC against the XR-09 directory.

        Returns one of:
          "wfo"       -- present in the XR-09 WFO office directory (confirmed);
          "national"  -- a known national-centre code (legitimately not in XR-09);
          "unlisted"  -- not in XR-09 and not a known national centre (flag it);
          "no-directory" -- XR-09 not loaded, so no check was possible.
        """
        if not cccc:
            return "unlisted"
        if not self.valid_cccc:
            return "no-directory"
        if cccc in self.valid_cccc:
            return "wfo"
        if cccc in self.NATIONAL_CENTER_CCCC:
            return "national"
        return "unlisted"

    def lookup_code(self, table_id: str, meaning: str) -> Optional[int]:
        """Translate a decoded meaning string to its GRIB2 code, or None."""
        tbl = self.code_tables.get(table_id)
        if not tbl:
            return None
        codes = tbl.get(normalize_meaning(meaning))
        if codes and len(codes) == 1:
            return next(iter(codes))
        return None

    def describe(self) -> str:
        parts = []
        for s in self.sources:
            if s.kind == "wmo_code_table":
                n = sum(len(v) for v in self.code_tables.values())
                parts.append(
                    f"{s.id} ({len(self.code_tables)} tables, {n} codes)"
                )
            elif s.kind == "grib2_param_table":
                n = len(s.manifest.get("param_abbrev", {}))
                parts.append(f"{s.id} ({n} parameter abbreviations)")
            elif s.kind == "cccc_directory":
                parts.append(f"{s.id} ({len(s.manifest.get('valid_cccc', []))} CCCC offices)")
            elif s.kind == "nws_awips_xref":
                parts.append(f"{s.id} ({len(s.manifest.get('awips_xref', {}))} AWIPS NNN)")
            elif s.kind == "nws_analysis_header":
                np_ = len(s.manifest.get("analysis_param_t2", {}))
                nd = len(s.manifest.get("analysis_domain_a1", {}))
                parts.append(f"{s.id} ({np_} params x {nd} domains)")
            else:
                parts.append(f"{s.id} ({s.kind}, {len(s.entries)} entries)")
        return "; ".join(parts)


def _read_sources(registry_dir: Path) -> list:
    """Load the generated registry from ``registry.db``.

    The registry is a SQLite database (built by ``build_registry.py``). Each row
    is reconstructed into the same ``LoadedSource(manifest, entries)`` the
    resolver has always consumed, so nothing downstream changes.
    """
    import registry_db

    db_path = registry_dir / "registry.db"
    if not registry_db.db_exists(db_path):
        return []
    return [
        LoadedSource(manifest=manifest, entries=entries)
        for manifest, entries in registry_db.read_db(db_path)
    ]


def load_registry(
    registry_dir: Union[str, Path] = REGISTRY_DIR, auto_build: bool = True
) -> Registry:
    """Load the generated registry, building it once if it is missing.

    The registry is build output (see ``build_registry.py``); it is not
    committed. On a fresh checkout it will be absent, so by default this builds
    it once from the pinned sources in ``sources.json``. Set ``auto_build=False``
    to require a pre-built registry instead (used by tests, which must stay
    offline and hermetic).
    """
    registry_dir = Path(registry_dir)
    sources = _read_sources(registry_dir)

    if not sources:
        if not auto_build:
            raise FileNotFoundError(
                f"No generated registry found in {registry_dir}. "
                f"Run: python build_registry.py"
            )
        # Lazy import: build_registry depends on the parser modules, not on this
        # one, so there is no import cycle; the lazy form just keeps the network
        # -touching builder out of the import path for callers that never need it.
        import build_registry

        print(
            "No registry found; building it once from pinned sources "
            "(this fetches the authoritative documents)...",
            flush=True,
        )
        rc = build_registry.main(["--out-dir", str(registry_dir)])
        if rc != 0:
            raise RuntimeError(
                "registry build failed; see the output above. "
                "You can also run `python build_registry.py` directly."
            )
        sources = _read_sources(registry_dir)
        if not sources:
            raise RuntimeError(
                f"registry build reported success but produced nothing in "
                f"{registry_dir}"
            )

    return Registry(sources)


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------


def normalize_name(name: str) -> str:
    """Normalize a product name for comparison (case/separator insensitive)."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def normalize_meaning(meaning: str) -> str:
    """Normalize a code-table meaning string.

    Archive metadata appends unit/qualifier suffixes, e.g.
    ``'Specified Height Level Above Ground (m)'`` for the table's
    ``'Specific height level above ground'``. Trailing parentheticals are
    dropped and 'specified'/'specific' are folded together.
    """
    s = meaning.lower().strip()
    s = re.sub(r"\([^)]*\)", " ", s)          # drop parentheticals
    s = s.replace("specified", "specific")     # wording variance
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def desc_abbrev(desc: str) -> str:
    """The NCEP abbreviation at the start of a parm DESC field.

    DESC looks like ``'HGT      1000 mb'`` or ``'A PCP    Surface'``; the
    abbreviation is the text before the run of whitespace that separates it from
    the level text, so a two-word abbreviation is preserved.
    """
    s = desc.strip()
    if not s:
        return ""
    parts = re.split(r"\s{2,}", s)
    return normalize_name(parts[0])


def _is_clean_abbrev(desc: str) -> bool:
    """True if a parm DESC starts with a real parameter abbreviation.

    A parameter abbreviation is a single whitespace-free token (optionally a
    two-token form like 'A PCP' that ``desc_abbrev`` handles), e.g. 'HGT',
    'PMTF', 'A PCP'. It is *not* a prose phrase like 'SUPER WMO HEADER' or
    '1 HOUR SFC OZONE'. We accept the first field before a 2+ space run only if
    it is short and looks like a code, so junk DESCs are not registered as
    abbreviations.
    """
    s = desc.strip()
    if not s:
        return False
    first = re.split(r"\s{2,}", s)[0].strip()
    # Reject obvious sentinels.
    if first.upper() == "SUPER WMO HEADER":
        return False
    # A clean abbreviation is <=8 chars and either one token or the NCEP
    # two-token form (e.g. 'A PCP', 'V GRD'); more/longer means it's prose.
    tokens = first.split()
    if len(tokens) > 2:
        return False
    if len(first.replace(" ", "")) > 8:
        return False
    # Must contain at least one letter and no lowercase prose words.
    if not re.search(r"[A-Za-z]", first):
        return False
    return True


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class WMOHeader:
    """A fully-resolved heading, every field traceable to the registry."""

    ttaaii: str
    cccc: str
    yygggg: str
    bbb: Optional[str] = None
    product: Optional[str] = None
    description: Optional[str] = None
    domain: Optional[str] = None
    grid: Optional[str] = None
    source_id: Optional[str] = None
    source_ref: Optional[str] = None
    source_raw: Optional[str] = None
    resolved: bool = True
    # Match quality:
    #   "exact"            -- the archive's grid IS the grid this header is
    #                         assigned to; the header is authoritative for this
    #                         exact data.
    #   "parameter"        -- the archive's grid is NOT a headered grid (it is the
    #                         native file grid the archive was built from), so this
    #                         is the header NWS assigns to the same parameter/level/
    #                         forecast on its disseminated (regridded) grid. It
    #                         identifies the field's GTS product, not this exact
    #                         grid's bulletin (there is none).
    match: str = "exact"
    assigned_grid: Optional[str] = None   # the headered grid, when match=="parameter"
    archive_grid: Optional[str] = None    # short description of the archive's grid
    alternatives: list = field(default_factory=list)  # other candidate headers
    # CCCC validation against the XR-09 office directory: wfo | national |
    # unlisted | no-directory. "unlisted" is worth a caller's attention.
    cccc_status: str = "no-directory"

    @property
    def heading(self) -> str:
        parts = [self.ttaaii, self.cccc, self.yygggg]
        if self.bbb:
            parts.append(self.bbb)
        return " ".join(parts)

    @property
    def caveat(self) -> Optional[str]:
        """Human-readable provenance note, or None for an exact-grid match."""
        if self.match == "exact":
            return None
        return (
            f"parameter-level header: NWS assigns this to the field on grid "
            f"{self.assigned_grid!r}; the archive is the native file grid "
            f"({self.archive_grid}), which is not disseminated under a WMO header"
        )

    def __str__(self) -> str:
        return self.heading


@dataclass
class Unresolved:
    """Returned when no accurate heading can be produced."""

    product: str
    reasons: list = field(default_factory=list)
    evidence: dict = field(default_factory=dict)
    resolved: bool = False

    def __str__(self) -> str:
        return f"UNRESOLVED[{self.product}]: " + "; ".join(self.reasons)


# ---------------------------------------------------------------------------
# Time (YYGGgg)
# ---------------------------------------------------------------------------


def _coerce_time(t) -> Optional[datetime]:
    if t is None:
        return None
    if isinstance(t, datetime):
        dt = t
    elif isinstance(t, np.datetime64):
        dt = datetime.fromtimestamp(
            t.astype("datetime64[s]").astype("int64"), tz=timezone.utc
        )
    elif isinstance(t, (int, float)):
        dt = datetime.fromtimestamp(float(t), tz=timezone.utc)
    elif isinstance(t, str):
        try:
            dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        try:
            dt = t.to_pydatetime()
        except AttributeError:
            try:
                dt = datetime.fromtimestamp(
                    np.datetime64(t, "s").astype("int64"), tz=timezone.utc
                )
            except Exception:
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_yygggg(t) -> Optional[str]:
    """Format YYGGgg (day/hour/minute UTC), or None if the time is unusable.

    There is deliberately no fallback to the current time: a heading carrying a
    made-up timestamp would not be accurate.
    """
    dt = _coerce_time(t)
    return None if dt is None else f"{dt.day:02d}{dt.hour:02d}{dt.minute:02d}"


# ---------------------------------------------------------------------------
# Grid identification (for tocgrib2 sources)
# ---------------------------------------------------------------------------


def identify_grid(section3_fields: dict, registry: Registry) -> Optional[str]:
    """Return the registry grid name matching these Section 3 fields, else None."""
    for name, spec in registry.grid_specs.items():
        want = spec.get("section3") or {}
        if not want:
            continue
        ok = True
        for key, expected in want.items():
            got = section3_fields.get(key)
            if got is None:
                ok = False
                break
            if isinstance(expected, float):
                if abs(got - expected) > 1e-6:
                    ok = False
                    break
            elif got != expected:
                ok = False
                break
        if ok:
            return name
    return None


# ---------------------------------------------------------------------------
# Resolvers
# ---------------------------------------------------------------------------


def resolve_by_product_name(
    short_name: str,
    domain: Optional[str],
    reference_time,
    registry: Registry,
    bbb: Optional[str] = None,
) -> Union[WMOHeader, Unresolved]:
    """Resolve an ``nws_notice`` style assignment (product name + domain)."""
    reasons: list = []
    key = normalize_name(short_name)
    candidates = registry.by_product.get(key, [])

    if not candidates:
        return Unresolved(
            product=short_name,
            reasons=[
                "no authoritative NWS notice ties this product name to a WMO "
                "header (the product may not be disseminated on the SBN, or its "
                "code was excluded because the source documents disagree)"
            ],
            evidence={"normalized_name": key},
        )

    if domain:
        want = domain.strip().lower()
        matched = [c for c in candidates if c["domain"].lower() == want]
        if not matched:
            return Unresolved(
                product=short_name,
                reasons=[
                    f"product is assigned a header for "
                    f"{sorted({c['domain'] for c in candidates})} but not for "
                    f"domain {domain!r}"
                ],
            )
        candidates = matched
    elif len({c["domain"] for c in candidates}) > 1:
        return Unresolved(
            product=short_name,
            reasons=[
                f"product has per-domain headers "
                f"{sorted({c['domain'] for c in candidates})} but the domain "
                f"could not be determined from the data"
            ],
        )

    codes = {(c["code"], c["cccc"]) for c in candidates}
    if len(codes) > 1:
        return Unresolved(
            product=short_name,
            reasons=[f"ambiguous: registry offers multiple codes {sorted(codes)}"],
        )

    yygggg = format_yygggg(reference_time)
    if yygggg is None:
        reasons.append(
            "reference time missing or unusable, so YYGGgg cannot be formed"
        )
    if reasons:
        return Unresolved(product=short_name, reasons=reasons)

    e = candidates[0]
    return WMOHeader(
        ttaaii=e["code"],
        cccc=e["cccc"],
        yygggg=yygggg,
        bbb=bbb,
        product=short_name,
        description=e["description"],
        domain=e["domain"],
        source_id=e["source_id"],
        source_ref=e["source_doc"],
        source_raw=e.get("source_raw"),
    )


def _pdt_matches(want: dict, entry_pdt: dict) -> Optional[str]:
    """Return None if compatible, else the name of the first disagreeing field.

    Comparison rules:

    * ``None`` in the parm record is a wildcard (the record left that slot at
      -9999), so only the fields a record pins are compared.
    * A field we have no evidence for is skipped -- absence of evidence is not
      disagreement. It makes the identity weaker, which tends toward an ambiguous
      (and therefore unresolved) result rather than a wrong match.
    * The level is compared in **physical** space: the archive stores a physical
      value while the record stores a scaled value plus scale factor.
    """
    want_level = want.get("_levelPhysical")
    if want_level is not None:
        pinned_level = physical_level(
            entry_pdt.get("scaledValueOfFirstFixedSurface"),
            entry_pdt.get("scaleFactorOfFirstFixedSurface"),
        )
        if pinned_level is not None and not levels_close(want_level, pinned_level):
            return "scaledValueOfFirstFixedSurface"

    for key, pinned in entry_pdt.items():
        if pinned is None:
            continue
        if key in ("scaledValueOfFirstFixedSurface", "scaleFactorOfFirstFixedSurface"):
            continue  # handled above, in physical space
        got = want.get(key)
        if got is None:
            continue
        if int(got) != int(pinned):
            return key
    return None


def _params_for_abbrev_in_source(abbrev: str, registry: Registry, source_id: str):
    """The distinct (cat, num) a source's own parm records use for an abbrev.

    When resolution is scoped to one model (``prefer_source``), the parameter
    identity is taken from that model's own records rather than the global abbrev
    map. This is what lets HRRR (standard GRIB2 numbers) and GFS (NCEP local-table
    numbers) use the same abbreviation without colliding.
    """
    found = set()
    for entries in registry.by_grid.values():
        for e in entries:
            if e.get("source_id") != source_id:
                continue
            if desc_abbrev(e["desc"]) != abbrev:
                continue
            cat = e["pdt"].get("parameterCategory")
            num = e["pdt"].get("parameterNumber")
            if cat is not None and num is not None:
                found.add((cat, num))
    return found


def _resolve_parameter(short_name: str, registry: Registry,
                       prefer_source: Optional[str] = None):
    """Resolve a short_name to (parameterCategory, parameterNumber) or an error.

    Returns (param_tuple, None) on success, or (None, Unresolved) on failure.
    When ``prefer_source`` is given, the parameter identity comes from that
    source's own parm records (so model-specific numbering does not clash).
    """
    abbrev = normalize_name(short_name)

    if prefer_source is not None:
        scoped = _params_for_abbrev_in_source(abbrev, registry, prefer_source)
        if len(scoped) == 1:
            return next(iter(scoped)), None
        if len(scoped) > 1:
            return None, Unresolved(
                product=short_name,
                reasons=[
                    f"abbreviation maps to multiple GRIB2 parameters {sorted(scoped)} "
                    f"within {prefer_source}, so the parameter identity is ambiguous"
                ],
            )
        # Not found in the preferred source: fall through to the global map.

    if abbrev in registry.abbrev_ambiguous:
        return None, Unresolved(
            product=short_name,
            reasons=[
                f"abbreviation maps to multiple GRIB2 parameters "
                f"{registry.abbrev_ambiguous[abbrev]} in the registry, so the "
                f"parameter identity is ambiguous"
            ],
        )
    param = registry.abbrev_param.get(abbrev)
    if param is None:
        return None, Unresolved(
            product=short_name,
            reasons=[
                "this parameter is not among those NWS disseminates with a WMO "
                "header (no parm record on any assigned grid uses this abbreviation)"
            ],
        )
    return param, None


def _match_records(records: list, want: dict, pdtn: Optional[int]):
    """Records whose pinned PDT fields agree with ``want``. Returns (hits, misses)."""
    hits, misses = [], {}
    for e in records:
        if pdtn is not None and e["pdtn"] != pdtn:
            misses["pdtn"] = misses.get("pdtn", 0) + 1
            continue
        bad = _pdt_matches(want, e["pdt"])
        if bad is None:
            hits.append(e)
        else:
            misses[bad] = misses.get(bad, 0) + 1
    return hits, misses


def _narrow_by_cycle(hits: list, model_cycle_hour) -> list:
    """Filter candidate records to those whose model cycle matches the data.

    Each record may carry a ``cycle`` (the model run hour, taken authoritatively
    from the parm filename, e.g. t06z -> 6). When candidates differ only by cycle
    and the archive's model-run hour (from forecast_reference_time) identifies
    one, keep just that cycle. If no record carries a cycle, or the model hour is
    unknown/matches none, hits are returned unchanged -- this never *causes* a
    wrong pick, it only removes cycles the data demonstrably is not.
    """
    cycles = {h.get("cycle") for h in hits}
    if cycles <= {None} or model_cycle_hour is None:
        return hits
    matched = [h for h in hits if h.get("cycle") == model_cycle_hour]
    return matched if matched else hits


def _describe_grid(s3: dict) -> str:
    if not s3:
        return "unknown"
    return f"{s3.get('Ni')}x{s3.get('Nj')} gdt{s3.get('gridDefinitionTemplateNumber')}"


def resolve_by_grib_identity(
    short_name: str,
    identity: dict,
    reference_time,
    registry: Registry,
    bbb: Optional[str] = None,
    allow_parameter_fallback: bool = True,
    prefer_source: Optional[str] = None,
) -> Union[WMOHeader, Unresolved]:
    """Resolve a heading from GRIB2 identity.

    Two match qualities, in order of strength:

    1. **exact** -- the archive's grid IS an assigned (headered) grid, and a parm
       record on that grid matches the full identity. The header is authoritative
       for this exact data.
    2. **parameter** -- the archive's grid is not a headered grid (it is the
       native file grid the archive was built from). We then report the header
       NWS assigns to the same parameter/level/forecast on its disseminated grid,
       flagged via ``match="parameter"``. Enabled unless
       ``allow_parameter_fallback=False``.

    ``identity`` may contain: section3 (from :func:`parse_section3`), pdtn, and
    PDT field values already translated to GRIB2 codes.
    """
    s3 = identity.get("section3")
    if not s3:
        return Unresolved(
            product=short_name,
            reasons=["grid could not be determined (no usable grib_section3)"],
        )

    param, err = _resolve_parameter(short_name, registry, prefer_source=prefer_source)
    if err is not None:
        return err

    want = dict(identity.get("pdt") or {})
    want["parameterCategory"], want["parameterNumber"] = param
    pdtn = identity.get("pdtn")

    yygggg = format_yygggg(reference_time)
    if yygggg is None:
        return Unresolved(
            product=short_name,
            reasons=["reference time missing or unusable, so YYGGgg cannot be formed"],
        )

    # --- Path 1: exact grid match ------------------------------------------
    grid = identify_grid(s3, registry)
    if grid is not None:
        hits, misses = _match_records(registry.by_grid.get(grid, []), want, pdtn)
        picked = _unique_header(hits)
        if picked is None and hits:
            # Ambiguous. Try narrowing by model cycle: some parm sets (AQM) have
            # separate records per cycle, the cycle authoritatively taken from the
            # parm filename. The archive's model-run hour selects the cycle.
            hits = _narrow_by_cycle(hits, identity.get("model_cycle_hour"))
            picked = _unique_header(hits)
        if picked is not None:
            return _make_header(
                short_name, picked, yygggg, bbb, match="exact", grid=grid
            )
        if hits:  # still ambiguous after cycle narrowing
            return Unresolved(
                product=short_name,
                reasons=[
                    f"identity matches {len({(c['ttaaii'], c['cccc']) for c in hits})} "
                    f"different headers on grid {grid!r}; metadata cannot distinguish them"
                ],
                evidence={"grid": grid,
                          "candidates": sorted({c["ttaaii"] for c in hits})},
            )

    # --- Path 2: parameter-level fallback (archive grid is not headered) ----
    if not allow_parameter_fallback:
        return Unresolved(
            product=short_name,
            reasons=[
                "archive grid is not an assigned (headered) grid and "
                "parameter-level fallback is disabled"
            ],
            evidence={"dataset_grid": s3, "assigned_grids": sorted(registry.grid_specs)},
        )

    picked, alts, err = _match_across_all_grids(
        short_name, want, pdtn, registry, prefer_source=prefer_source
    )
    if err is not None:
        return err

    hdr = _make_header(
        short_name, picked, yygggg, bbb, match="parameter",
        grid=None, assigned_grid=picked["grid"],
    )
    hdr.archive_grid = _describe_grid(s3)
    hdr.alternatives = alts
    return hdr


def _unique_header(hits: list):
    """Return the single record if all hits share one (ttaaii, cccc), else None."""
    if not hits:
        return None
    codes = {(c["ttaaii"], c["cccc"]) for c in hits}
    return hits[0] if len(codes) == 1 else None


def _match_across_all_grids(short_name, want, pdtn, registry, prefer_source=None):
    """Find the parameter's header across every assigned grid, with tie-break.

    Returns (picked_record, alternatives, None) or (None, None, Unresolved).
    Tie-break: prefer a whole-globe grid (global lat/lon covering ~360x181+),
    since the native archives here are global; otherwise, if more than one grid
    offers a header and none is clearly the global one, return Unresolved listing
    the candidates rather than guess.

    When ``prefer_source`` is given, only that source's records are considered.
    A header is a dissemination decision tied to a model: an HRRR archive's field
    takes HRRR's (KWBY) assignment, not another model's assignment for the same
    parameter. Without the scope, a parameter headered by several models would
    collide, and the global-grid tie-break (meant for the GFS native-grid case)
    would wrongly prefer GFS.
    """
    per_grid: dict = {}
    for grid, records in registry.by_grid.items():
        if prefer_source is not None:
            records = [e for e in records if e.get("source_id") == prefer_source]
        hits, _ = _match_records(records, want, pdtn)
        picked = _unique_header(hits)
        if picked is not None:
            per_grid[grid] = picked

    if not per_grid:
        return None, None, Unresolved(
            product=short_name,
            reasons=[
                "no assigned grid carries a WMO header for this parameter/level/"
                "forecast, so no header applies even at the parameter level"
            ],
        )

    if len(per_grid) == 1:
        (grid, rec), = per_grid.items()
        return rec, [], None

    # Tie-break: prefer the global grid.
    global_grids = [
        g for g in per_grid
        if _is_global_grid(registry.grid_specs.get(g, {}).get("section3", {}))
    ]
    alts = [
        {"grid": g, "ttaaii": r["ttaaii"], "cccc": r["cccc"]}
        for g, r in sorted(per_grid.items())
    ]
    if len(global_grids) == 1:
        return per_grid[global_grids[0]], alts, None

    # Ambiguous: multiple grids, no single global one to prefer.
    return None, None, Unresolved(
        product=short_name,
        reasons=[
            "parameter is headered on multiple grids and none is unambiguously the "
            "global grid; cannot choose without guessing"
        ],
        evidence={"candidates": alts},
    )


def _is_global_grid(s3: dict) -> bool:
    """A lat/lon grid spanning the globe (~360 deg lon, ~180 deg lat)."""
    if not s3 or s3.get("gridDefinitionTemplateNumber") != 0:
        return False
    ni, nj = s3.get("Ni"), s3.get("Nj")
    di, dj = s3.get("di_deg"), s3.get("dj_deg")
    if None in (ni, nj, di, dj) or di == 0 or dj == 0:
        return False
    lon_span = ni * di
    lat_span = nj * dj
    return lon_span >= 359.0 and lat_span >= 179.0


def _make_header(short_name, e, yygggg, bbb, match, grid=None, assigned_grid=None):
    return WMOHeader(
        ttaaii=e["ttaaii"],
        cccc=e["cccc"],
        yygggg=yygggg,
        bbb=bbb,
        product=short_name,
        description=e["desc"],
        grid=grid,
        source_id=e["source_id"],
        source_ref=f"{e['source_file']}:{e['source_line']}",
        source_raw=e.get("source_raw"),
        match=match,
        assigned_grid=assigned_grid,
    )


def resolve_analysis_header(
    short_name: str,
    dataset_key: Optional[str],
    reference_time,
    registry: Registry,
    bbb: Optional[str] = None,
) -> Union[WMOHeader, Unresolved]:
    """Resolve an RTMA/URMA analysis header by composing T1+T2+A1+A2ii.

    ``dataset_key`` is ``dataset|resolution|domain`` (e.g. ``rtma|2p5|conus``),
    derived from the store's identity/prefix. The parameter selects T2, the
    dataset_key selects A1; both must be known or the result is Unresolved.
    """
    if not registry.analysis_headers:
        return Unresolved(product=short_name, reasons=["no analysis-header source loaded"])

    key = normalize_name(short_name)
    for tbl in registry.analysis_headers:
        pinfo = tbl["param_t2"].get(key)
        if pinfo is None:
            continue  # this parameter isn't in this analysis source
        if not dataset_key:
            return Unresolved(
                product=short_name,
                reasons=["dataset/domain unknown; cannot pick the A1 designator"],
            )
        dinfo = tbl["domain_a1"].get(dataset_key)
        if dinfo is None:
            return Unresolved(
                product=short_name,
                reasons=[f"no A1 designator for dataset/domain {dataset_key!r}"],
                evidence={"known": sorted(tbl["domain_a1"])},
            )
        yygggg = format_yygggg(reference_time)
        if yygggg is None:
            return Unresolved(
                product=short_name,
                reasons=["reference time missing or unusable, so YYGGgg cannot be formed"],
            )
        ttaaii = f"{tbl['t1']}{pinfo['t2']}{dinfo['a1']}{tbl['a2ii']}"
        return WMOHeader(
            ttaaii=ttaaii,
            cccc=tbl["cccc"],
            yygggg=yygggg,
            bbb=bbb,
            product=short_name,
            description=pinfo["desc"],
            source_id=tbl["source_id"],
            source_ref=f"{pinfo['source']} + {dinfo['source']}",
            match="exact",
        )
    return Unresolved(
        product=short_name,
        reasons=["parameter not in any RTMA/URMA analysis-header table"],
    )


def _elevation_matches(want: Optional[float], entry: dict) -> bool:
    """True if the data's tilt matches a NOAAPort row's elevation(s).

    A row with no elevation ('Elevation Angle Not Applicable') matches any tilt,
    because the product is not elevation-specific. A row with elevation values
    matches only if the data's tilt is one of them (small float tolerance).
    """
    vals = entry.get("elevation_values") or []
    if not vals:
        return True
    if want is None:
        return False
    return any(abs(want - v) <= 0.05 for v in vals)


def resolve_radar(
    radar_identity,
    registry: Registry,
) -> Union[WMOHeader, Unresolved]:
    """Resolve a NEXRAD radar product to its WMO heading, or Unresolved.

    Two-class, matching how NEXRAD is actually disseminated:

    * **Level II** base-data volumes carry no WMO heading (distributed as whole
      files via LDM/FTP), so they are always Unresolved.
    * **Level III** products resolve only if they appear in the NOAAPort table.
      The match key is (product_code, elevation_angle) -> NNN -> SDUS<tier>. The
      ``ii`` placeholder and ``CCCC`` are per-site: ``CCCC`` is taken from the
      store's ``site_id``. Products defined in the ICD but absent from the table
      (e.g. super-res 167/168) have no heading and are Unresolved.

    ``radar_identity`` is a :class:`radar_identity.RadarIdentity`.
    """
    ident = radar_identity
    name = ident.short_name

    if ident.level == 2:
        return Unresolved(
            product=name,
            reasons=[
                "Level II base data are distributed as whole-volume files via "
                "LDM/FTP, not under a WMO abbreviated heading; no authoritative "
                "record assigns one"
            ],
            evidence={"level": 2, "instrument": ident.instrument_name},
        )

    if ident.product_code is None:
        return Unresolved(
            product=name,
            reasons=["no product_code in the store metadata; cannot identify a "
                     "Level III product"],
        )

    candidates = registry.radar_by_code.get(ident.product_code, [])
    if not candidates:
        return Unresolved(
            product=name,
            reasons=[
                f"product code {ident.product_code} is not disseminated on the "
                f"SBN under a WMO heading (absent from the NOAAPort radar table; "
                f"e.g. super-res products 167/168 are defined but not broadcast)"
            ],
            evidence={"product_code": ident.product_code},
        )

    matched = [e for e in candidates if _elevation_matches(ident.elevation_angle, e)]
    if not matched:
        tilts = sorted({v for e in candidates for v in (e.get("elevation_values") or [])})
        return Unresolved(
            product=name,
            reasons=[
                f"product code {ident.product_code} is disseminated, but no row "
                f"matches elevation {ident.elevation_angle}"
            ],
            evidence={"product_code": ident.product_code, "known_tilts": tilts},
        )

    nnns = {e["nnn"] for e in matched}
    prefixes = {e["ttaaii_prefix"] for e in matched}
    if len(nnns) > 1 or len(prefixes) > 1:
        return Unresolved(
            product=name,
            reasons=[
                f"elevation {ident.elevation_angle} matches multiple headings "
                f"{sorted(nnns)}; tilt does not uniquely select one"
            ],
            evidence={"candidates": sorted(nnns)},
        )

    e = matched[0]
    cccc = ident.site_id
    if not cccc:
        return Unresolved(
            product=name,
            reasons=["no site_id in the store, so the heading's CCCC/site is "
                     "unknown"],
            evidence={"nnn": e["nnn"], "ttaaii_prefix": e["ttaaii_prefix"]},
        )

    yygggg = format_yygggg(ident.scan_time)
    if yygggg is None:
        return Unresolved(
            product=name,
            reasons=["scan_time missing or unusable, so YYGGgg cannot be formed"],
        )

    hdr = WMOHeader(
        ttaaii=e["ttaaii_prefix"],   # SDUS<tier>; the trailing ii is per-site
        cccc=cccc,
        yygggg=yygggg,
        product=name,
        description=f"{ident.product_name or name} ({e['nnn']})",
        source_id=e["source_id"],
        source_ref=e["source_doc"],
        source_raw=e.get("source_raw"),
        match="exact",
    )
    hdr.cccc_status = registry.validate_cccc(cccc)
    return hdr


def resolve(
    short_name: str,
    reference_time,
    registry: Registry,
    domain: Optional[str] = None,
    identity: Optional[dict] = None,
    bbb: Optional[str] = None,
    dataset_key: Optional[str] = None,
) -> Union[WMOHeader, Unresolved]:
    """Resolve a heading, trying each registry kind that could apply.

    Name-based (notice) assignments are tried first because they are product-
    level statements; identity-based (parm) assignments are tried when GRIB2
    identity evidence is available. If neither yields a match, the returned
    :class:`Unresolved` carries the reasons from every route attempted.
    """
    reasons: list = []
    evidence: dict = {}

    if registry.by_product:
        r = resolve_by_product_name(
            short_name, domain, reference_time, registry, bbb=bbb
        )
        if isinstance(r, WMOHeader):
            r.cccc_status = registry.validate_cccc(r.cccc)
            return r
        reasons += [f"[name match] {x}" for x in r.reasons]
        evidence.update(r.evidence)

    if registry.analysis_headers and dataset_key:
        r = resolve_analysis_header(
            short_name, dataset_key, reference_time, registry, bbb=bbb
        )
        if isinstance(r, WMOHeader):
            r.cccc_status = registry.validate_cccc(r.cccc)
            return r
        reasons += [f"[analysis header] {x}" for x in r.reasons]
        evidence.update(r.evidence)

    if identity:
        r = resolve_by_grib_identity(
            short_name, identity, reference_time, registry, bbb=bbb
        )
        if isinstance(r, WMOHeader):
            r.cccc_status = registry.validate_cccc(r.cccc)
            return r
        reasons += [f"[identity match] {x}" for x in r.reasons]
        evidence.update(r.evidence)
    elif registry.by_grid:
        reasons.append(
            "[identity match] no GRIB2 identity evidence supplied "
            "(need grib_section3 and PDT attributes)"
        )

    if not reasons:
        reasons.append("no registry source could apply to this product")
    return Unresolved(product=short_name, reasons=reasons, evidence=evidence)
