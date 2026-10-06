"""Resolve WMO abbreviated headings for the products in an icechunk store.

Thin CLI over :mod:`grib_identity` (metadata -> GRIB2 identity) and
:mod:`wmo_header` (identity -> authoritative heading).

A heading is printed only when every field traces to an authoritative record in
``registry/``. Anything else prints as UNRESOLVED with the reason -- never a
partial or guessed heading.

Note that one archived variable can correspond to many GRIB2 messages (one per
level per time), each with its own heading, so results fan out over those axes.

Usage::

    python read_icechunk.py                          # MRMS (default store)
    python read_icechunk.py --prefix <path-to.ic>
    python read_icechunk.py --bucket B --prefix P
    python read_icechunk.py --domain conus           # override domain evidence
    python read_icechunk.py --all-times --all-levels
    python read_icechunk.py --explain                # show matching evidence

Credentials come from the environment; the store is opened read-only.
"""

from __future__ import annotations

import argparse
import sys
import warnings

import icechunk
import numpy as np
import xarray as xr

from grib_identity import extract_identity, message_pdt
from radar_identity import extract_radar_identity, is_radar_store
from wmo_header import Unresolved, load_registry, resolve, resolve_radar

# The icechunk rust client emits harmless CA-cert warnings on this host.
warnings.filterwarnings("ignore")

DEFAULT_BUCKET = "wsupviewer-eks-transfer"
DEFAULT_PREFIX = "edr-api/mrms_v12p2/2025-09/mrms_v12p2-conus_lambert-2025-09.ic"

# Domain keywords recognized in a store prefix. Evidence only: with no keyword the
# domain stays unknown and name-based matching reports that rather than assuming.
DOMAIN_KEYWORDS = {
    "conus": "CONUS",
    "alaska": "Alaska",
    "hawaii": "Hawaii",
    "guam": "Guam",
    "caribbean": "Caribbean",
    "carib": "Caribbean",
    "puerto": "Caribbean",
}


def open_store(bucket: str, prefix: str, region: str, branch: str) -> xr.DataTree:
    storage = icechunk.s3_storage(
        bucket=bucket, prefix=prefix, region=region, from_env=True
    )
    repo = icechunk.Repository.open(storage)
    return xr.open_datatree(
        repo.readonly_session(branch).store,
        engine="zarr",
        chunks=None,
        consolidated=False,
    )


def open_local_store(path: str, branch: str) -> xr.DataTree:
    storage = icechunk.local_filesystem_storage(path)
    repo = icechunk.Repository.open(storage)
    return xr.open_datatree(
        repo.readonly_session(branch).store,
        engine="zarr",
        chunks=None,
        consolidated=False,
    )


def run_radar(dt: xr.DataTree, registry) -> int:
    """Resolve every product group of a CF/Radial radar store.

    Radar fans out over product/sweep groups, not level x time: each group is one
    product (L3) or one sweep's moments (L2). L2 and non-disseminated L3 products
    resolve to Unresolved by design -- that is the correct answer, not a gap.
    """
    root = dt["/"].to_dataset().attrs
    n_ok = 0
    unresolved: list = []

    for gpath in sorted(dt.groups):
        if gpath == "/":
            continue  # root holds site metadata; radar products live in subgroups
        ds = dt[gpath].to_dataset()
        if not ds.data_vars:
            continue  # container group (e.g. /KTLX), no product here
        ident = extract_radar_identity(ds, root)
        result = resolve_radar(ident, registry)

        if isinstance(result, Unresolved):
            unresolved.append((gpath, result))
            print(f"UNRESOLVED: {ident.short_name}  (group {gpath})")
            for r in result.reasons:
                print(f"     {r}")
            print()
            continue

        n_ok += 1
        print(f"WMO: {result.heading}")
        print(f"     product     : {ident.product_name or ident.short_name}")
        print(f"     code         : {ident.product_code}")
        print(f"     description : {result.description}")
        print(f"     elevation    : {ident.elevation_angle}")
        print(f"     site (CCCC)  : {result.cccc}")
        print(f"     authority    : {result.source_id} -> {result.source_ref}")
        if result.cccc_status == "unlisted":
            print(f"     note         : CCCC {result.cccc} not in XR-09 directory "
                  f"(radar site codes are not WFO nodes)")
        print(f"     source group : {gpath}")
        print()

    print(f"Resolved {n_ok} heading(s); {len(unresolved)} product(s) unresolved.")
    return 0


def infer_domain(text: str):
    low = (text or "").lower()
    for kw, label in DOMAIN_KEYWORDS.items():
        if kw in low:
            return label
    return None


def infer_dataset_key(prefix: str):
    """Derive an analysis dataset key 'dataset|resolution|domain' from a prefix.

    e.g. 'edr-api/rtma2p5_v2p10/...conus_lambert...' -> 'rtma|2p5|conus'.
    Returns None if the prefix does not look like an RTMA/URMA analysis store.
    """
    low = (prefix or "").lower()
    dataset = "rtma" if "rtma" in low else ("urma" if "urma" in low else None)
    if dataset is None:
        return None
    res = "2p5" if ("2p5" in low or "2.5" in low) else (
        "5" if "5km" in low else None)
    domain = None
    for kw, canon in (("conus", "conus"), ("alaska", "alaska"), ("hawaii", "hawaii"),
                      ("puerto", "puertorico"), ("guam", "guam")):
        if kw in low:
            domain = canon
            break
    if res is None or domain is None:
        return None
    return f"{dataset}|{res}|{domain}"


def iter_variables(dt: xr.DataTree):
    for gpath in sorted(dt.groups):
        ds = dt[gpath].to_dataset()
        for var_name, da in ds.data_vars.items():
            yield gpath, var_name, da, ds


def select(values: list, want_all: bool, index):
    """Choose which axis values to report on."""
    if not values:
        return [None]
    if want_all:
        return values
    if index is not None:
        return [values[index]]
    return [values[-1]]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bucket", default=DEFAULT_BUCKET)
    p.add_argument("--prefix", default=DEFAULT_PREFIX)
    p.add_argument("--region", default="us-east-1")
    p.add_argument("--branch", default="main")
    p.add_argument("--domain", default=None, help="Domain override (e.g. conus).")
    p.add_argument("--explain", action="store_true", help="Show matching evidence.")
    p.add_argument("--limit", type=int, default=None, help="Stop after N headings.")
    p.add_argument("--all-levels", action="store_true", help="Every vertical level.")
    p.add_argument("--level-index", type=int, default=None)
    p.add_argument("--local", default=None,
                   help="Path to a local-filesystem icechunk store (e.g. a radar "
                        "store on /efs). Bypasses S3.")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--time-index", type=int, default=None)
    g.add_argument("--all-times", action="store_true")
    args = p.parse_args(argv)

    registry = load_registry()

    if args.local:
        print(f"Opening {args.local} (branch={args.branch})")
        dt = open_local_store(args.local, args.branch)
    else:
        print(f"Opening s3://{args.bucket}/{args.prefix} (branch={args.branch})")
        dt = open_store(args.bucket, args.prefix, args.region, args.branch)

    print(f"Registry: {registry.describe()}")

    # Radar (CF/Radial) stores use the radar path, which fans out over
    # product/sweep groups rather than GRIB2 level x time.
    if is_radar_store(dt):
        print("Store type: NEXRAD radar (CF/Radial)\n")
        return run_radar(dt, registry)

    domain = args.domain or infer_domain(args.prefix)
    dataset_key = infer_dataset_key(args.prefix)
    print(f"Domain evidence: {domain or 'undetermined'}\n")

    n_ok = 0
    unresolved: dict = {}
    # Archives often store a resolution pyramid (groups /1 /2 /4 /8) of the same
    # product. Those are renderings of one message, not separate bulletins, so an
    # identical result is reported once. The key includes the heading and the
    # level/time it came from, so genuinely distinct results are never collapsed.
    emitted: dict = {}
    done = False

    for gpath, var_name, da, ds in iter_variables(dt):
        ident = extract_identity(da, ds, registry)
        src = gpath if gpath.rstrip("/").endswith(var_name) else f"{gpath}/{var_name}"

        times = select(ident.times, args.all_times, args.time_index)
        levels = select(ident.levels, args.all_levels, args.level_index)

        for t in times:
            # lead time is parallel to the time axis when present
            lead = None
            if ident.lead_hours and t is not None and ident.times:
                try:
                    lead = ident.lead_hours[ident.times.index(t)]
                except (ValueError, IndexError):
                    lead = None

            for lvl in levels:
                result = resolve(
                    ident.short_name,
                    reference_time=t,
                    registry=registry,
                    domain=domain,
                    dataset_key=dataset_key,
                    identity={
                        "section3": ident.section3,
                        "pdtn": ident.pdtn,
                        "pdt": message_pdt(ident, lvl, lead),
                        "model_cycle_hour": ident.model_cycle_hour,
                    },
                )

                if isinstance(result, Unresolved):
                    # Report once per product to keep output readable.
                    if ident.short_name not in unresolved:
                        unresolved[ident.short_name] = result
                        print(f"UNRESOLVED: {ident.short_name}")
                        for r in result.reasons:
                            print(f"     {r}")
                        if args.explain:
                            for k, v in result.evidence.items():
                                print(f"     evidence: {k} = {v}")
                            for note in ident.notes:
                                print(f"     note: {note}")
                        print(f"     source group: {src}")
                        print()
                    continue

                key = (result.heading, ident.short_name, repr(lvl), str(t))
                if key in emitted:
                    emitted[key].append(src)
                    continue
                emitted[key] = [src]

                n_ok += 1
                tstr = np.datetime_as_string(np.datetime64(t), unit="m") if t is not None else "n/a"
                print(f"WMO: {result.heading}")
                print(f"     product     : {ident.short_name}")
                print(f"     description : {result.description}")
                if result.domain:
                    print(f"     domain      : {result.domain}")
                if result.grid:
                    print(f"     grid        : {result.grid}")
                if result.match == "parameter":
                    print(f"     match       : parameter-level (see note)")
                    print(f"     note        : {result.caveat}")
                    if args.explain and result.alternatives:
                        for a in result.alternatives:
                            print(f"     alt         : {a['ttaaii']} {a['cccc']} on {a['grid']}")
                if lvl is not None:
                    print(f"     level       : {lvl} {ident.level_units or ''}".rstrip())
                print(f"     valid (UTC) : {tstr}")
                print(f"     authority   : {result.source_id} -> {result.source_ref}")
                if result.cccc_status == "unlisted":
                    print(f"     WARNING     : CCCC {result.cccc} not in the XR-09 office directory")
                elif result.cccc_status in ("wfo", "national") and args.explain:
                    print(f"     cccc check  : {result.cccc} ({result.cccc_status}, per XR-09)")
                if args.explain and result.source_raw:
                    print(f"     source text : {result.source_raw[:150]}")
                print(f"     source group: {src}")
                print()

                if args.limit and n_ok >= args.limit:
                    done = True
                    break

            if done:
                break
        if done:
            break

    collapsed = sum(len(v) - 1 for v in emitted.values())
    print(f"Resolved {n_ok} heading(s); {len(unresolved)} product(s) unresolved.")
    if collapsed:
        print(
            f"({collapsed} duplicate result(s) from resolution-pyramid groups "
            f"collapsed; use --explain to see source groups)"
        )
    if unresolved:
        print(f"Unresolved products: {sorted(unresolved)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
