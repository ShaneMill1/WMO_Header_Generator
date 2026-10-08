"""Extract GRIB2 product identity from archived (zarr/icechunk) metadata.

A WMO heading is assigned per **GRIB2 message**, and one archived variable can
span many messages: a variable on 34 isobaric levels across 39 forecast times is
1,326 messages, each with its own heading (e.g. ``YHPB99`` at 1000 hPa versus
``YHPB85`` at 850 hPa). So identity extraction yields the *axes* of that fan-out
(levels, times) alongside the fields shared by every message.

What the archives preserve, and what they do not:

============================  =========================================
GRIB2 element                 Archived as
============================  =========================================
Section 3 (grid)              raw integer array (``grib_section3``)
Section 4 template number     prose, e.g. "... (see Template 4.0)"
Type of fixed surface         prose, e.g. "Isobaric Surface (Pa)"
Type of generating process    prose, e.g. "Forecast"
Statistical process           prose, e.g. "Average"
Level value                   a coordinate, in physical units
Parameter category/number     **not preserved** -- only ``short_name``
Discipline                    **not preserved**
Originating centre number     **not preserved** -- only its name
============================  =========================================

The prose fields are translated back to GRIB2 codes through the WMO code tables
in the registry; a string that does not map to exactly one code is omitted,
which weakens the identity (making an unresolved result more likely) rather than
risking a wrong one. The missing parameter identity is why the parameter must be
inferred from ``short_name`` -- the one genuinely inferential step in the chain.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

# Index positions within the stored ``grib_section3`` array. Verified against the
# separately-stored scalar attributes (latitude_first_gridpoint,
# longitude_first_gridpoint, gridlength_x_direction, gridlength_y_direction) on
# both the MRMS and GFS archives.
S3_TEMPLATE = 4
S3_NI = 12
S3_NJ = 13
S3_LA1 = 16
S3_LO1 = 17
S3_DI = 21
S3_DJ = 22
MICRO = 1e6

# Authoritative marker that a coordinate carries the first fixed surface. Present
# on both scalar levels (MRMS) and dimension levels (GFS isobaric), so it is used
# instead of guessing from coordinate names.
FIRST_SURFACE_KEY = "valueOfFirstFixedSurface"

TEMPLATE_RE = re.compile(r"Template\s+4\.(\d+)")


def parse_section3(section3) -> Optional[dict]:
    """Extract the comparable grid fields from a stored ``grib_section3`` array."""
    if section3 is None:
        return None
    if isinstance(section3, str):
        try:
            section3 = json.loads(section3)
        except json.JSONDecodeError:
            return None
    try:
        vals = [int(v) for v in section3]
    except (TypeError, ValueError):
        return None
    if len(vals) <= S3_DJ:
        return None
    return {
        "gridDefinitionTemplateNumber": vals[S3_TEMPLATE],
        "Ni": vals[S3_NI],
        "Nj": vals[S3_NJ],
        "lat_first": vals[S3_LA1] / MICRO,
        "lon_first": vals[S3_LO1] / MICRO,
        "di_deg": vals[S3_DI] / MICRO,
        "dj_deg": vals[S3_DJ] / MICRO,
    }


def physical_level(scaled_value, scale_factor) -> Optional[float]:
    """Physical level value from a GRIB2 scaled value and scale factor.

    GRIB2 encodes a level as ``scaledValue * 10**-scaleFactor``.
    """
    if scaled_value is None:
        return None
    factor = 0 if scale_factor is None else scale_factor
    try:
        return float(scaled_value) * (10.0 ** -float(factor))
    except (TypeError, ValueError, OverflowError):
        return None


def levels_close(a: Optional[float], b: Optional[float]) -> bool:
    """Compare two physical level values.

    Both sides originate from the same integer encoding, so this only needs to
    absorb float round-trip error, not genuine tolerance.
    """
    if a is None or b is None:
        return False
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-6)


@dataclass
class ProductIdentity:
    """GRIB2 identity evidence for one archived variable.

    ``levels`` and ``times`` are the fan-out axes: each combination corresponds
    to a distinct GRIB2 message and therefore potentially a distinct heading.
    """

    short_name: str
    section3: Optional[dict] = None
    pdtn: Optional[int] = None
    pdt: dict = field(default_factory=dict)
    # Level axis
    level_coord: Optional[str] = None
    level_units: Optional[str] = None
    levels: list = field(default_factory=list)   # physical values
    # Time axis
    times: list = field(default_factory=list)
    lead_hours: list = field(default_factory=list)  # parallel to times, or empty
    # Model run cycle hour (UTC) from forecast_reference_time, e.g. 6 for t06z.
    # Distinct from the valid time; used to disambiguate per-cycle headers.
    model_cycle_hour: Optional[int] = None
    # Statistical window length in hours, from a 'duration' coordinate, e.g. 1
    # for a 1-hour average. Distinguishes interval products of the same field.
    duration_hours: Optional[float] = None
    notes: list = field(default_factory=list)

    def shared_pdt(self) -> dict:
        """PDT fields common to every message of this variable."""
        return dict(self.pdt)


def _find_level_coord(ds):
    """Return (name, variable) of the first-fixed-surface coordinate, or (None, None).

    Identified solely by the ``grib_name`` attribute, which names the GRIB2 keys
    the coordinate was decoded from. Guessing from coordinate names was rejected:
    a wrong guess would inject a wrong level into the identity and could match a
    wrong heading.
    """
    for name, var in ds.coords.items():
        gname = str(var.attrs.get("grib_name", ""))
        if FIRST_SURFACE_KEY in gname:
            return name, var
    return None, None


# ---------------------------------------------------------------------------
# CF-encoded GRIB2 (the "CIRRUS" dialect)
# ---------------------------------------------------------------------------
#
# Some archives (e.g. the CIRRUS HRRR stores) do not keep the raw GRIB2 Section 3
# array or the PDT prose. Instead they record the same facts in CF form:
#
#   grid           -> a CF grid-mapping on a ``spatial_ref`` coordinate, plus
#                     projected ``x``/``y`` axes;
#   level          -> ``scaled_value_of_first_fixed_surface`` /
#                     ``scale_factor_of_first_fixed_surface`` coordinates;
#   surface type   -> the group's ``code`` attribute (GRIB2 Table 4.5 value);
#   parameter      -> the variable name (``short_name``);
#   reference time -> an ``init_time`` coordinate;
#   forecast hour  -> a ``lead_time`` (timedelta) coordinate.
#
# This is still GRIB2 identity, just a different serialization, so it feeds the
# same resolver. It is handled by a separate extractor rather than by branching
# the GRIB-array extractor, to keep each dialect's assumptions isolated.

# GRIB2 grid definition template numbers, by CF grid_mapping_name.
_CF_GRID_MAPPING_TO_GDTN = {
    "latitude_longitude": 0,
    "lambert_conformal_conic": 30,
    "polar_stereographic": 20,
    "mercator": 10,
}


def _find_spatial_ref(ds, root=None):
    """Return the CF grid-mapping coordinate, from the group or the root.

    The CIRRUS stores keep ``spatial_ref`` on the root group and the per-level
    product groups inherit the x/y axes but not the grid-mapping, so both places
    must be checked.
    """
    sr = ds.coords.get("spatial_ref")
    if sr is None and root is not None:
        sr = root.coords.get("spatial_ref")
    return sr


def is_cf_grib_dataset(ds, root=None) -> bool:
    """True if a dataset looks like CF-encoded GRIB2 (vs the grib_section3 form).

    Keyed on the markers the CIRRUS writer leaves and the GRIB-array writer does
    not: a ``spatial_ref`` CF grid-mapping (on this group or the root) together
    with the scaled-value level coordinates. Never guesses from variable names.
    """
    has_spatial_ref = _find_spatial_ref(ds, root) is not None
    has_scaled_level = "scaled_value_of_first_fixed_surface" in ds.coords
    return bool(has_spatial_ref and has_scaled_level)


def _section3_from_cf(ds, root=None) -> Optional[dict]:
    """Build a Section-3-equivalent grid dict from a CF grid-mapping.

    Returns the comparable fields :func:`parse_section3` would produce, so the
    resolver's grid logic can treat both encodings uniformly. The grid-mapping
    name sets the template number; ``x``/``y`` set the dimensions.

    Note: this is the *archive* grid. For the HRRR CIRRUS stores it is the native
    3 km grid, which is not an assigned (headered) grid, so the resolver will
    correctly fall through to a parameter-level match rather than an exact one.
    """
    sr = _find_spatial_ref(ds, root)
    if sr is None:
        return None
    mapping = str(sr.attrs.get("grid_mapping_name", ""))
    gdtn = _CF_GRID_MAPPING_TO_GDTN.get(mapping)
    if gdtn is None:
        return None

    out: dict = {"gridDefinitionTemplateNumber": gdtn}
    coords = ds.coords if "x" in ds.coords else (root.coords if root is not None else ds.coords)
    try:
        if "x" in coords and "y" in coords:
            out["Ni"] = int(np.atleast_1d(coords["x"].values).size)
            out["Nj"] = int(np.atleast_1d(coords["y"].values).size)
    except (TypeError, ValueError):
        pass
    return out


def _levels_from_cf(ds, ident: ProductIdentity) -> None:
    """Fill the level axis from the scaled-value / scale-factor coordinates."""
    if "scaled_value_of_first_fixed_surface" not in ds.coords:
        ident.notes.append("no scaled_value_of_first_fixed_surface coordinate")
        return
    scaled = np.atleast_1d(ds.coords["scaled_value_of_first_fixed_surface"].values)
    if "scale_factor_of_first_fixed_surface" in ds.coords:
        factors = np.atleast_1d(ds.coords["scale_factor_of_first_fixed_surface"].values)
    else:
        factors = np.zeros_like(scaled)
    levels = []
    for sv, sf in zip(scaled, factors):
        try:
            levels.append(physical_level(int(sv), int(sf)))
        except (TypeError, ValueError):
            levels.append(None)
    ident.levels = [lv for lv in levels if lv is not None]
    ident.level_coord = "scaled_value_of_first_fixed_surface"


def extract_identity_cf(da, ds, registry, group_attrs: Optional[dict] = None,
                        root=None) -> ProductIdentity:
    """Build GRIB2 identity from a CF-encoded (CIRRUS) dataset.

    The counterpart of :func:`extract_identity` for stores that carry a CF
    grid-mapping instead of ``grib_section3``. ``group_attrs`` is the enclosing
    group's attributes, which carry the surface-type ``code`` (GRIB2 Table 4.5).
    ``root`` is the root-group dataset, which holds the ``spatial_ref``
    grid-mapping the product groups inherit implicitly.
    """
    a = da.attrs
    ga = group_attrs or ds.attrs
    ident = ProductIdentity(
        short_name=a.get("short_name") or str(da.name),
        section3=_section3_from_cf(ds, root),
    )
    # These stores are GRIB2 PDT template 4.0 (instantaneous) and 4.8 (interval).
    # The template number is not recorded per-variable; leave it unset so the
    # resolver matches on parameter/level/forecast rather than pinning a template
    # we cannot prove. (A record's own pdtn still filters candidates.)
    ident.pdtn = None

    # Surface type (GRIB2 Table 4.5) from the group 'code' attribute.
    code = ga.get("code")
    if code is not None:
        try:
            ident.pdt["typeOfFirstFixedSurface"] = int(code)
        except (TypeError, ValueError):
            ident.notes.append(f"group code {code!r} is not an integer surface type")

    _levels_from_cf(ds, ident)

    # Reference time from init_time; forecast hour from lead_time.
    if "init_time" in ds.coords:
        ident.times = list(np.atleast_1d(ds.coords["init_time"].values))
        try:
            frt = np.atleast_1d(ds.coords["init_time"].values)[0]
            ident.model_cycle_hour = int(frt.astype("datetime64[h]").astype("int64") % 24)
        except (TypeError, ValueError, IndexError):
            pass
    if "lead_time" in ds.coords:
        try:
            leads = np.atleast_1d(ds.coords["lead_time"].values)
            ident.lead_hours = [float(x / np.timedelta64(1, "h")) for x in leads]
        except (TypeError, ValueError, ZeroDivisionError):
            ident.notes.append("lead_time could not be converted to hours")

    if ident.section3 is None:
        ident.notes.append("could not derive a grid from the CF spatial_ref")
    return ident


def extract_identity(da, ds, registry) -> ProductIdentity:
    """Build GRIB2 identity evidence for one variable from its metadata.

    Parameters
    ----------
    da
        The data variable (``xarray.DataArray``).
    ds
        Its enclosing dataset, used for coordinates.
    registry
        A loaded registry, used for WMO code-table lookups.
    """
    a = da.attrs
    ident = ProductIdentity(
        short_name=a.get("short_name") or str(da.name),
        section3=parse_section3(a.get("grib_section3")),
    )

    m = TEMPLATE_RE.search(a.get("product_definition_template_number") or "")
    if m:
        ident.pdtn = int(m.group(1))
    else:
        ident.notes.append("PDT template number not stated in metadata")

    # Prose -> GRIB2 codes via the WMO code tables.
    for attr, table in (
        ("type_of_generating_process", "4.3"),
        ("type_of_first_fixed_surface", "4.5"),
        ("statistical_process", "4.10"),
    ):
        text = a.get(attr)
        if not text:
            continue
        key = {
            "type_of_generating_process": "typeOfGeneratingProcess",
            "type_of_first_fixed_surface": "typeOfFirstFixedSurface",
            "statistical_process": "statisticalProcess",
        }[attr]
        code = registry.lookup_code(table, text)
        if code is None:
            ident.notes.append(
                f"{attr}={text!r} did not map to exactly one code in table {table}"
            )
        else:
            ident.pdt[key] = code

    # Level axis, from the authoritative grib_name marker.
    lname, lvar = _find_level_coord(ds)
    if lvar is not None:
        ident.level_coord = str(lname)
        ident.level_units = lvar.attrs.get("units")
        try:
            ident.levels = [float(v) for v in np.atleast_1d(lvar.values)]
        except (TypeError, ValueError):
            ident.notes.append(f"level coordinate {lname!r} has non-numeric values")
    else:
        ident.notes.append(
            "no coordinate carries a 'valueOfFirstFixedSurface' grib_name, "
            "so the level is unknown"
        )

    # Time axis and the parallel lead times.
    if "time" in da.coords:
        ident.times = list(np.atleast_1d(da.coords["time"].values))

    # Model run cycle, from forecast_reference_time (distinct from valid time).
    if "forecast_reference_time" in ds.coords:
        try:
            frt = np.atleast_1d(ds.coords["forecast_reference_time"].values)[0]
            ident.model_cycle_hour = int(
                frt.astype("datetime64[h]").astype("int64") % 24
            )
        except (TypeError, ValueError, IndexError):
            ident.notes.append("forecast_reference_time could not be read")

    # Statistical window length, from a 'duration' coordinate (timedelta).
    if "duration" in ds.coords:
        try:
            dur = np.atleast_1d(ds.coords["duration"].values)[0]
            ident.duration_hours = float(dur / np.timedelta64(1, "h"))
        except (TypeError, ValueError, ZeroDivisionError):
            ident.notes.append("duration could not be converted to hours")

    if "lead_time" in ds.coords:
        try:
            leads = np.atleast_1d(ds.coords["lead_time"].values)
            hours = [float(x / np.timedelta64(1, "h")) for x in leads]
            if len(hours) == 1 and ident.times:
                hours = hours * len(ident.times)
            ident.lead_hours = hours
        except (TypeError, ValueError, ZeroDivisionError):
            ident.notes.append("lead_time could not be converted to hours")

    return ident


def message_pdt(ident: ProductIdentity, level: Optional[float], lead_h: Optional[float]) -> dict:
    """PDT field values for one concrete message of a variable.

    The level is carried as a physical value (``_levelPhysical``) rather than a
    scaled integer, because the archive stores physical units while the parm
    records store a scaled value plus a scale factor; comparison happens in
    physical space.
    """
    pdt = ident.shared_pdt()
    if level is not None:
        pdt["_levelPhysical"] = level
    if lead_h is not None and float(lead_h).is_integer():
        pdt["forecastTime"] = int(lead_h)
        pdt["indicatorOfUnitOfTimeRange"] = 1  # hour
    # Statistical accumulation/averaging window length, for interval templates
    # (4.8/4.11/4.12). Distinguishes e.g. 1-hour vs 8-hour ozone average.
    if ident.duration_hours is not None and float(ident.duration_hours).is_integer():
        pdt["lengthOfTimeRange"] = int(ident.duration_hours)
        pdt["indicatorOfUnitForTimeRange"] = 1  # hour
    return pdt
