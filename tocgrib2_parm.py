"""Parser for NCEP ``tocgrib2`` GRIBIDS parm files.

These parm files are the authoritative source of WMO abbreviated headings for
NWS/NCEP GRIB2 products: they are the operational configuration that the
``tocgrib2`` utility reads to stamp headers onto disseminated products. They are
not documentation *about* headers -- they *are* the header assignment.

From ``NCEPLIBS-grib_util/src/tocgrib2/tocgrib2.F90``::

    NAMELIST /GRIBIDS/ DSCPL, IDS, GDTN, GDT, PDTN, PDT, DESC, WMOHEAD, EXTRACT
    ...
    CALL MAKWMO (WMOHEAD(1:6), dayofmonth, hourofday, WMOHEAD(8:11), WMOHDR)

so ``WMOHEAD(1:6)`` is T1T2A1A2ii, ``WMOHEAD(8:11)`` is CCCC, and YYGGgg is
taken from the GRIB message at dissemination time.

Record syntax (one Fortran namelist group per line)::

    &GRIBIDS DESC=' HGT      1000 mb ',WMOHEAD='YHPB99 KWBC',PDTN= 0 ,PDT=  3 5 2 0 96 0 0 1 6 100 0 100000 255 0 0 /

Notes on the format, verified against the GFS v16.3.24 parm set (62,605 records
across 350 files):

* ``PDT`` is a whitespace-separated integer list whose meaning depends on
  ``PDTN`` (the GRIB2 Product Definition Template number).
* Fortran repeat syntax appears in the time-interval templates, e.g.
  ``6*-9999`` meaning six consecutive ``-9999`` values.
* ``-9999`` is a wildcard: "match any value in this slot".
* Neither ``DSCPL`` (discipline) nor ``GDTN``/``GDT`` (grid) appear in the GFS
  parm files. The grid is *not* part of the parm record; it is bound by the
  dissemination script, which regrids the model output before invoking
  ``tocgrib2`` with a specific parm file. That binding must therefore be
  supplied separately (see ``registry/sources.json``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

# Sentinel used in the parm files to mean "any value".
WILDCARD = -9999

# GRIB2 Product Definition Template field names, in PDT order (the values that
# follow octet 9 of Section 4). Names follow the WMO GRIB2 template definitions
# / eccodes key names.
#
# Template 4.0: analysis or forecast at a horizontal level at a point in time.
PDT_4_0 = [
    "parameterCategory",
    "parameterNumber",
    "typeOfGeneratingProcess",
    "backgroundGeneratingProcessIdentifier",
    "generatingProcessIdentifier",
    "hoursAfterDataCutoff",
    "minutesAfterDataCutoff",
    "indicatorOfUnitOfTimeRange",
    "forecastTime",
    "typeOfFirstFixedSurface",
    "scaleFactorOfFirstFixedSurface",
    "scaledValueOfFirstFixedSurface",
    "typeOfSecondFixedSurface",
    "scaleFactorOfSecondFixedSurface",
    "scaledValueOfSecondFixedSurface",
]

# The end-of-interval + statistical-processing tail shared by the interval
# templates (4.8, 4.11, 4.12).
_STAT_INTERVAL_TAIL = [
    "yearOfEndOfOverallTimeInterval",
    "monthOfEndOfOverallTimeInterval",
    "dayOfEndOfOverallTimeInterval",
    "hourOfEndOfOverallTimeInterval",
    "minuteOfEndOfOverallTimeInterval",
    "secondOfEndOfOverallTimeInterval",
    "numberOfTimeRanges",
    "numberOfMissingValues",
    "statisticalProcess",
    "typeOfTimeIncrement",
    "indicatorOfUnitForTimeRange",
    "lengthOfTimeRange",
    "indicatorOfUnitForTimeIncrement",
    "timeIncrement",
]

# Template 4.8: statistically processed over a time interval. Extends 4.0 with
# the end-of-interval timestamp and the statistical-processing description.
PDT_4_8 = PDT_4_0 + _STAT_INTERVAL_TAIL

# Template 4.15: average/accumulation over a spatial area at a point in time.
PDT_4_15 = PDT_4_0 + [
    "statisticalProcess",
    "typeOfSpatialProcessing",
    "numberOfPointsUsed",
]

# Template 4.12: derived forecast based on ensemble members, over a time
# interval (used by GEFS AWIPS products). Template 4.0 core, then the ensemble
# derived-forecast fields, then the statistical time-interval tail.
PDT_4_12 = PDT_4_0 + [
    "derivedForecast",
    "numberOfForecastsInEnsemble",
] + _STAT_INTERVAL_TAIL

PDT_TEMPLATES = {0: PDT_4_0, 8: PDT_4_8, 12: PDT_4_12, 15: PDT_4_15}


@dataclass
class ParmRecord:
    """One &GRIBIDS record: a GRIB2 identity plus its assigned WMO heading."""

    desc: str                 # free-text description, e.g. ' HGT      1000 mb '
    ttaaii: str               # WMOHEAD(1:6),  e.g. 'YHPB99'
    cccc: str                 # WMOHEAD(8:11), e.g. 'KWBC'
    pdtn: int                 # Product Definition Template number
    pdt: list                 # raw PDT integers (WILDCARD preserved)
    # provenance
    path: str = ""
    line_no: int = 0
    raw: str = ""
    discipline: Optional[int] = None   # DSCPL if present
    gdtn: Optional[int] = None         # GDTN if present
    gdt: list = field(default_factory=list)

    @property
    def wmohead(self) -> str:
        return f"{self.ttaaii} {self.cccc}"

    def pdt_fields(self) -> dict:
        """PDT as a name->value dict, with wildcards as None.

        ``tocgrib2`` initializes the whole PDT array to -9999 before reading each
        namelist record, so any slot the record does not supply is a wildcard.
        Short PDT lists are therefore legal and mean "match anything in the
        remaining slots"; we pad accordingly.
        """
        names = PDT_TEMPLATES.get(self.pdtn)
        out: dict = {}
        if names is None:
            for i, v in enumerate(self.pdt):
                out[f"pdt[{i}]"] = None if v == WILDCARD else v
            return out
        for i, name in enumerate(names):
            v = self.pdt[i] if i < len(self.pdt) else WILDCARD
            out[name] = None if v == WILDCARD else v
        return out

    def short_name(self) -> str:
        """The NCEP abbreviation from DESC (first whitespace-delimited token).

        DESC is free text of the form ``' HGT      1000 mb '``. Some entries use
        a two-word abbreviation (e.g. ``' A PCP    Surface '``), so this is a
        convenience for reporting, not a matching key.
        """
        return self.desc.strip().split()[0] if self.desc.strip() else ""


def _expand_fortran_list(text: str) -> list:
    """Expand a Fortran namelist integer list, handling ``n*value`` repeats.

    e.g. '1 0 1 2 6*-9999 1' -> [1, 0, 1, 2, -9999 x6, 1]
    """
    values: list = []
    for tok in text.replace(",", " ").split():
        if "*" in tok:
            count_s, _, val_s = tok.partition("*")
            try:
                count = int(count_s)
                val = int(val_s)
            except ValueError:
                raise ValueError(f"bad Fortran repeat token {tok!r}")
            values.extend([val] * count)
        else:
            try:
                values.append(int(tok))
            except ValueError:
                raise ValueError(f"bad integer token {tok!r}")
    return values


_RE_DESC = re.compile(r"DESC\s*=\s*'([^']*)'")
_RE_WMOHEAD = re.compile(r"WMOHEAD\s*=\s*'([^']*)'")
_RE_PDTN = re.compile(r"PDTN\s*=\s*(-?\d+)")
_RE_DSCPL = re.compile(r"DSCPL\s*=\s*(-?\d+)")
_RE_GDTN = re.compile(r"GDTN\s*=\s*(-?\d+)")
# PDT= runs to the next KEY= or the closing slash.
_RE_PDT = re.compile(
    r"PDT\s*=\s*(.*?)(?=(?:,\s*)?[A-Z]+\s*=|/\s*$)", re.DOTALL
)
_RE_GDT = re.compile(r"GDT\s*=\s*(.*?)(?=(?:,\s*)?[A-Z]+\s*=|/\s*$)", re.DOTALL)


def parse_line(line: str, path: str = "", line_no: int = 0) -> Optional[ParmRecord]:
    """Parse one &GRIBIDS line. Returns None for blank/comment/non-record lines."""
    s = line.strip()
    if not s or s.startswith("!") or "GRIBIDS" not in s.upper():
        return None

    m_wmo = _RE_WMOHEAD.search(s)
    m_pdtn = _RE_PDTN.search(s)
    m_pdt = _RE_PDT.search(s)
    if not (m_wmo and m_pdtn and m_pdt):
        return None

    wmohead = m_wmo.group(1)
    # WMOHEAD is 'TTAAii CCCC' -- 6 chars, space, 4 chars.
    parts = wmohead.split()
    if len(parts) != 2 or len(parts[0]) != 6 or len(parts[1]) != 4:
        raise ValueError(f"{path}:{line_no}: unexpected WMOHEAD {wmohead!r}")
    ttaaii, cccc = parts

    m_desc = _RE_DESC.search(s)
    m_dscpl = _RE_DSCPL.search(s)
    m_gdtn = _RE_GDTN.search(s)
    m_gdt = _RE_GDT.search(s)

    return ParmRecord(
        desc=m_desc.group(1) if m_desc else "",
        ttaaii=ttaaii,
        cccc=cccc,
        pdtn=int(m_pdtn.group(1)),
        pdt=_expand_fortran_list(m_pdt.group(1)),
        path=path,
        line_no=line_no,
        raw=s,
        discipline=int(m_dscpl.group(1)) if m_dscpl else None,
        gdtn=int(m_gdtn.group(1)) if m_gdtn else None,
        gdt=_expand_fortran_list(m_gdt.group(1)) if m_gdt else [],
    )


def parse_text(text: str, path: str = "") -> list:
    """Parse all &GRIBIDS records in a parm file's text."""
    records = []
    for i, line in enumerate(text.splitlines(), start=1):
        rec = parse_line(line, path=path, line_no=i)
        if rec is not None:
            records.append(rec)
    return records


def validate(records: list) -> list:
    """Return a list of human-readable problems found in parsed records.

    A record is a problem if we cannot interpret its identity, because that could
    lead to a wrong header:

    * unknown PDT template -- we do not know what the integers mean;
    * PDT longer than the template -- our template definition must be wrong.

    A PDT *shorter* than the template is legal (trailing slots are wildcards,
    see :meth:`ParmRecord.pdt_fields`) and is not reported.
    """
    problems = []
    for r in records:
        names = PDT_TEMPLATES.get(r.pdtn)
        if names is None:
            problems.append(
                f"{r.path}:{r.line_no}: unknown PDT template {r.pdtn} "
                f"(cannot interpret identity)"
            )
            continue
        if len(r.pdt) > len(names):
            problems.append(
                f"{r.path}:{r.line_no}: PDTN={r.pdtn} template defines "
                f"{len(names)} values but record supplies {len(r.pdt)}"
            )
    return problems
