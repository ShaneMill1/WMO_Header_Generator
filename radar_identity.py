"""Extract NEXRAD radar product identity from archived (CF/Radial) metadata.

Radar stores are not GRIB2, so this is the radar counterpart of
``grib_identity`` -- it reads the CF/Radial attributes a radar icechunk store
carries and produces a small identity object the radar resolver can match.

Two outcomes exist for radar, and the identity makes clear which applies:

Level III (per-product bulletins)
    Each store/group is one product (``product_code``) for one site at one
    elevation. The WMO heading, if any, is assigned per **product code +
    elevation angle** (one code maps to one NNN mnemonic per tilt). Resolvable
    when the product is SBN-disseminated.

Level II (whole-volume base data)
    One store holds all sweeps/moments of a volume. There is no ``product_code``
    (moments live in per-sweep groups), and Level II is distributed as whole
    files over LDM/FTP, not under a WMO heading. These are reported with
    ``level=2`` so the resolver can return a precise Unresolved rather than
    attempt a lookup.

What is read (never guessed):

=====================  ======================================================
CF/Radial attribute    Meaning
=====================  ======================================================
``product_code``       NEXRAD Level III product code (e.g. 165), group attr
``moment_name``        short moment label (e.g. DHC, HC), group attr
``product_name``       human product description, group attr
``site_id``            originating radar (becomes the heading's CCCC/site)
``scan_time``          volume/scan time -> YYGGgg
``instrument_name``    e.g. NEXRAD (root attr on L2 stores)
``elevation_angle``    per-sweep tilt coordinate; selects the NNN mnemonic
``Conventions``        'CF/Radial' marks a radar store
=====================  ======================================================
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np

CF_RADIAL = "CF/Radial"


@dataclass
class RadarIdentity:
    """Radar identity evidence for one product/moment of a store.

    ``level`` is 2 or 3. For Level III, ``product_code`` and ``elevation_angle``
    are the match key; for Level II they are typically absent, which the resolver
    treats as "no WMO heading assigned".
    """

    level: Optional[int] = None
    product_code: Optional[int] = None
    moment_name: Optional[str] = None
    product_name: Optional[str] = None
    site_id: Optional[str] = None
    scan_time: Optional[str] = None
    elevation_angle: Optional[float] = None
    instrument_name: Optional[str] = None
    notes: list = field(default_factory=list)

    @property
    def short_name(self) -> str:
        """A display label, mirroring ProductIdentity.short_name."""
        return self.moment_name or self.product_name or (
            f"product_{self.product_code}" if self.product_code is not None else "radar"
        )


def is_radar_store(dt) -> bool:
    """True if the datatree looks like a CF/Radial radar store.

    Checks ``Conventions`` / ``instrument_name`` on any group rather than
    guessing from group names, so a non-radar store is never misrouted. L2 stores
    carry these on the root; L3 stores carry them on the product group, so both
    the root and groups are checked.
    """
    def _looks_radar(attrs) -> bool:
        conv = str(attrs.get("Conventions", ""))
        instr = str(attrs.get("instrument_name", ""))
        return CF_RADIAL.lower() in conv.lower() or "nexrad" in instr.lower()

    try:
        for gpath in dt.groups:
            if _looks_radar(dt[gpath].to_dataset().attrs):
                return True
    except Exception:
        return False
    return False


def _first(values) -> Optional[float]:
    try:
        arr = np.atleast_1d(values)
        if arr.size == 0:
            return None
        return float(arr.ravel()[0])
    except (TypeError, ValueError):
        return None


def _infer_level(ds) -> int:
    """Level III iff the group names a product_code; else Level II."""
    if ds.attrs.get("product_code") is not None:
        return 3
    return 2


def extract_radar_identity(ds, root_attrs: Optional[dict] = None) -> RadarIdentity:
    """Build radar identity from one group's dataset.

    ``ds`` is the product/sweep group (``DataTree[path].to_dataset()``);
    ``root_attrs`` is the root group's attrs, carrying site metadata on Level II
    stores where the sweep groups do not repeat it.
    """
    a = dict(ds.attrs)
    root_attrs = root_attrs or {}

    ident = RadarIdentity(
        level=_infer_level(ds),
        moment_name=a.get("moment_name"),
        product_name=a.get("product_name"),
        site_id=a.get("site_id") or root_attrs.get("site_id"),
        scan_time=a.get("scan_time") or root_attrs.get("scan_time"),
        instrument_name=a.get("instrument_name") or root_attrs.get("instrument_name"),
    )

    pc = a.get("product_code")
    if pc is not None:
        try:
            ident.product_code = int(pc)
        except (TypeError, ValueError):
            ident.notes.append(f"product_code={pc!r} is not an integer")

    if "elevation_angle" in ds.coords:
        ident.elevation_angle = _first(ds.coords["elevation_angle"].values)
    elif "fixed_angle" in a:
        ident.elevation_angle = _first(a.get("fixed_angle"))

    if ident.level == 2:
        ident.notes.append(
            "Level II base data: distributed as whole-volume files via LDM/FTP, "
            "not under a WMO heading"
        )

    return ident
