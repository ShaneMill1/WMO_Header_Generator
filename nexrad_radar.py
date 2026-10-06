"""Parser for the NOAAPort Radar Products table (WSR-88D / TDWR).

Source: NWS "Radar Products Through the NOAAPort Satellite Broadcast" table
(weather.gov/media/tg/noaaport_radar_products.pdf). It is the authoritative
public record of which NEXRAD Level III products are disseminated on the SBN and
under what WMO heading. This is the radar analog of the MRMS SBN notices.

Each product row, after pypdf text extraction, looks like::

    {product_code}/{RPG}  SDUS{tier}i cccc  {NNN} xxx  {elevation}

e.g. ``161/DCC SDUS8i cccc NXC xxx -0.2``. One product code maps to several NNN
mnemonics, one per elevation tilt. We emit one entry per (product_code, NNN)
pairing, carrying the elevation text so the resolver can match on tilt.

Only products that appear in this table have a WMO heading. Products defined in
the WSR-88D ICD but absent here (e.g. super-res codes 167/168) are, correctly,
not emitted -- so they resolve to Unresolved.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

# A product row: "<code>/<rpg> SDUS<tier>i cccc <NNN> xxx <elevation...>".
# NNN is a 3-char AWIPS mnemonic. The WMO heading token also appears as NOUS/NXUS
# for status/text products; we capture the T1T2 generically.
ROW_RE = re.compile(
    r"\b(?P<code>\d{1,3})\s*/\s*(?P<rpg>[A-Z0-9]{1,4})\s+"
    r"(?P<tt>[A-Z]{4})(?P<tier>\d)(?P<i>i)\s+cccc\s+"
    r"(?P<nnn>[A-Z0-9]{3})\s+xxx\s*"
    r"(?P<elev>-?\d[\d.,\s]*|Elevation\s+Angle\s+Not\s+Applicable)?",
    re.I,
)


@dataclass
class RadarProductEntry:
    """One (product_code, NNN) heading assignment from the NOAAPort table."""

    product_code: int
    rpg_header: str          # e.g. DCC
    nnn: str                 # AWIPS mnemonic, e.g. NXC
    t1t2: str                # e.g. SD (of SDUS)
    tier: str                # the tier digit, e.g. '8'
    ttaaii_prefix: str       # e.g. SDUS8  (the i placeholder is per-site)
    elevation: Optional[str] = None   # verbatim elevation text, or None
    radar_kind: str = "WSR-88D"       # WSR-88D | TDWR
    raw: str = ""

    @property
    def elevation_values(self) -> list:
        """Parsed numeric elevations (may be several), or [] if N/A."""
        if not self.elevation:
            return []
        if "not applicable" in self.elevation.lower():
            return []
        out = []
        for tok in re.split(r"[,\s]+", self.elevation.strip()):
            try:
                out.append(float(tok))
            except ValueError:
                continue
        return out


def parse_noaaport_radar(text: str) -> list:
    """Extract product rows from the NOAAPort radar table text.

    pypdf splits each table row across two physical lines: the code + WMO heading
    ends with ``cccc`` on one line, and ``NNN xxx <elevation>`` follows on the
    next. We walk the lines in order, join those pairs, and track the WSR-88D vs
    TDWR section so a duplicate NNN under TDWR stays distinct from its WSR-88D
    namesake.
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    entries: list = []
    radar_kind = "WSR-88D"

    for i, cur in enumerate(lines):
        # The standalone "TDWR products" section header (not the title line
        # "(WSR-88D and TDWR PRODUCTS)"), which begins the TDWR block.
        if re.fullmatch(r"TDWR\s+products", cur, re.I):
            radar_kind = "TDWR"
            continue
        if not re.search(r"[A-Z]{4}\d\s*i\s+cccc\s*$", cur, re.I):
            continue
        if i + 1 >= len(lines):
            continue
        row = cur + " " + lines[i + 1]
        m = ROW_RE.search(row)
        if not m:
            continue
        tt = m.group("tt").upper()
        tier = m.group("tier")
        entries.append(
            RadarProductEntry(
                product_code=int(m.group("code")),
                rpg_header=m.group("rpg").upper(),
                nnn=m.group("nnn").upper(),
                t1t2=tt[:2],
                tier=tier,
                ttaaii_prefix=f"{tt}{tier}",
                elevation=(m.group("elev") or "").strip() or None,
                radar_kind=radar_kind,
                raw=row.strip(),
            )
        )
    return entries
