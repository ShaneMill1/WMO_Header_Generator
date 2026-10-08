"""Inventory and diff the metadata an icechunk store exposes.

This is a *metadata* comparison, not a header-resolution survey. It answers:
"what does a store tell you about its data -- which groups, variables,
attributes, coordinates, and grid encoding does it carry?" That is exactly what
decides what the resolver can key on, so comparing two store families (e.g. the
CIRRUS stores on /efs vs the nwsviz stores in S3) surfaces where their metadata
conventions diverge.

Two modes:

    # 1. Inventory one store (or several) into a JSON dump.
    python tools/metadata_inventory.py inventory \
        --local /efs/cirrus_test_data/HRRR-2026-09-21-sample-v2.icechunk \
        --local /efs/cirrus_test_data/KTLX_165_0.5_icechunk \
        --out cirrus_meta.json

    python tools/metadata_inventory.py inventory \
        --bucket wsupviewer-eks-transfer \
        --prefix edr-api/mrms_v12p2/2021-01/mrms_v12p2-conus_lambert-2021-01.ic \
        --prefix edr-api/gfs0p25_v16p3/2021-01-01T00:00/gfs0p25_v16p3-global_latlon-2021-01-01T00:00.ic \
        --out nwsviz_meta.json

    # 2. Diff two dumps (the gap analysis).
    python tools/metadata_inventory.py diff cirrus_meta.json nwsviz_meta.json

The inventory captures, per store: root/global attrs, and for every group every
variable's attribute keys, dtype, dims, and coordinate encoding -- plus a small
set of derived "signals" (grid encoding, level encoding, time encoding) that name
the conventions directly. S3 opens need AWS credentials in the environment; local
stores need none.
"""

from __future__ import annotations

import argparse
import json
import sys
import warnings
from collections import Counter

import icechunk
import numpy as np
import xarray as xr

warnings.filterwarnings("ignore")

# Values that are short and categorical enough to be worth carrying into the
# diff (so it can report "same key, different value"). Long/opaque values
# (arrays, prose) are summarized rather than dumped.
_MAX_VALUE_LEN = 120


# --------------------------------------------------------------------------- #
# opening                                                                     #
# --------------------------------------------------------------------------- #

def open_local(path: str, branch: str) -> xr.DataTree:
    storage = icechunk.local_filesystem_storage(path)
    repo = icechunk.Repository.open(storage)
    return xr.open_datatree(
        repo.readonly_session(branch).store,
        engine="zarr", chunks=None, consolidated=False,
    )


def open_s3(bucket: str, prefix: str, region: str, branch: str) -> xr.DataTree:
    storage = icechunk.s3_storage(
        bucket=bucket, prefix=prefix, region=region, from_env=True
    )
    repo = icechunk.Repository.open(storage)
    return xr.open_datatree(
        repo.readonly_session(branch).store,
        engine="zarr", chunks=None, consolidated=False,
    )


# --------------------------------------------------------------------------- #
# metadata extraction                                                         #
# --------------------------------------------------------------------------- #

def _jsonable(value):
    """Reduce an attribute value to something JSON-serializable and compact.

    Scalars pass through; arrays/lists become a shape+dtype summary; long
    strings are truncated. The goal is a stable, diffable fingerprint, not a
    faithful copy of the data.
    """
    if isinstance(value, (str, bool, int)):
        s = str(value)
        return s if len(s) <= _MAX_VALUE_LEN else f"<str len={len(s)}>"
    if isinstance(value, float):
        return value
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, (list, tuple, np.ndarray)):
        arr = np.asarray(value)
        return f"<array shape={tuple(arr.shape)} dtype={arr.dtype}>"
    return f"<{type(value).__name__}>"


def _attrs_fingerprint(attrs: dict) -> dict:
    """Map attr name -> compact value for every attribute, sorted by name."""
    return {k: _jsonable(v) for k, v in sorted(attrs.items())}


def _var_record(da: xr.DataArray) -> dict:
    return {
        "dtype": str(da.dtype),
        "dims": list(da.dims),
        "shape": [int(n) for n in da.shape],
        "attr_keys": sorted(da.attrs),
        "attrs": _attrs_fingerprint(da.attrs),
    }


def _coord_record(coord: xr.DataArray) -> dict:
    return {
        "dtype": str(coord.dtype),
        "dims": list(coord.dims),
        "size": int(coord.size),
        "attr_keys": sorted(coord.attrs),
        "attrs": _attrs_fingerprint(coord.attrs),
    }


def _derive_signals(dt: xr.DataTree) -> dict:
    """Name the encoding conventions a store uses, for a direct side-by-side.

    These are the things the resolver actually cares about, lifted out of the
    raw attr dump so the diff can speak in terms of conventions rather than
    individual keys.
    """
    all_var_attrs: Counter = Counter()
    all_group_attrs: Counter = Counter()
    all_coord_names: Counter = Counter()
    grid_encoding = set()
    level_encoding = set()
    time_encoding = set()
    identity_location = set()
    has_data_groups = 0

    root = dt["/"].to_dataset()
    root_attr_keys = set(root.attrs)

    # grid: grib_section3 array (GRIB2-array stores) vs CF spatial_ref grid-mapping
    for gpath in dt.groups:
        ds = dt[gpath].to_dataset()
        if gpath != "/":
            for k in ds.attrs:
                all_group_attrs[k] += 1
        if "spatial_ref" in ds.coords or "spatial_ref" in ds.variables:
            grid_encoding.add("cf_spatial_ref")
        for v in ds.variables:
            if "grib_section3" in str(v).lower():
                grid_encoding.add("grib_section3_array")
        for name, da in ds.data_vars.items():
            if "grib_section3" in da.attrs:
                grid_encoding.add("grib_section3_attr")
        for cname in ds.coords:
            all_coord_names[str(cname)] += 1
            low = str(cname).lower()
            if low in ("x", "y") or "projection_x" in low or "projection_y" in low:
                grid_encoding.add("projected_xy")
            if low in ("latitude", "longitude", "lat", "lon"):
                grid_encoding.add("latlon_coords")
            if "lead_time" in low or "lead" == low:
                time_encoding.add("lead_time_coord")
            if "init_time" in low or "reference_time" in low or "forecast_reference_time" in low:
                time_encoding.add("init_time_coord")
            if low == "time" or "valid_time" in low:
                time_encoding.add("time_coord")
            if "level" in low or "height" in low or "isobaric" in low or "pressure" in low:
                level_encoding.add(f"coord:{cname}")

        if ds.data_vars and gpath != "/":
            has_data_groups += 1
        for name, da in ds.data_vars.items():
            for k in da.attrs:
                all_var_attrs[k] += 1
            # scaled-value level encoding (CIRRUS CF form) shows up as attrs
            for k in da.attrs:
                lk = k.lower()
                if "scale_factor" in lk and "value" in lk:
                    level_encoding.add("scaled_value_attrs")
            if "short_name" in da.attrs:
                all_var_attrs["__has_short_name__"] += 1

    # radar signal: look across all groups, not just root. L2 puts site metadata
    # on the root (instrument_name/site_id/...); L3 nests it under /<SITE>/...,
    # with radial coords (azimuth/range/elevation_angle) on the product group.
    radar_markers = {"instrument_name", "radar_name", "site_id", "vcp_number"}
    radial_coords = {"azimuth", "range", "elevation_angle", "sweep"}
    is_radarish = (
        bool(radar_markers & (root_attr_keys | set(all_group_attrs)))
        or bool(radial_coords & set(all_coord_names))
        or any("sweep" in str(g).lower() for g in dt.groups)
    )
    if is_radarish:
        grid_encoding.add("radar_cf_radial")

    # where the identifying metadata lives -- a real convention difference:
    # GRIB2-array vars carry short_name on the variable; CIRRUS HRRR carries
    # code/name on the group; radar carries site/product on root or nested group.
    if "__has_short_name__" in all_var_attrs:
        identity_location.add("variable_attrs")
    if {"code", "name"} & set(all_group_attrs):
        identity_location.add("group_attrs")
    if radar_markers & root_attr_keys:
        identity_location.add("root_attrs")

    return {
        "root_attr_keys": sorted(root_attr_keys),
        "n_groups": len(list(dt.groups)),
        "n_data_groups": has_data_groups,
        "grid_encoding": sorted(grid_encoding),
        "level_encoding": sorted(level_encoding),
        "time_encoding": sorted(time_encoding),
        "identity_location": sorted(identity_location),
        "common_var_attr_keys": sorted(k for k in all_var_attrs if not k.startswith("__")),
        "common_group_attr_keys": sorted(all_group_attrs),
        "coord_names": sorted(all_coord_names),
    }


def inventory_store(dt: xr.DataTree, label: str) -> dict:
    groups = {}
    for gpath in sorted(dt.groups):
        ds = dt[gpath].to_dataset()
        groups[gpath] = {
            "attr_keys": sorted(ds.attrs),
            "attrs": _attrs_fingerprint(ds.attrs),
            "coords": {str(c): _coord_record(ds[c]) for c in sorted(ds.coords)},
            "data_vars": {
                str(v): _var_record(ds[v]) for v in sorted(ds.data_vars)
            },
        }
    return {
        "label": label,
        "signals": _derive_signals(dt),
        "groups": groups,
    }


# --------------------------------------------------------------------------- #
# diff                                                                        #
# --------------------------------------------------------------------------- #

def _collect_var_attr_keys(store: dict) -> set:
    keys = set()
    for g in store["groups"].values():
        for v in g["data_vars"].values():
            keys.update(v["attr_keys"])
    return keys


def _collect_coord_names(store: dict) -> set:
    names = set()
    for g in store["groups"].values():
        names.update(g["coords"].keys())
    return names


def _store_summary(dump: dict) -> dict:
    """Union of signals/keys/coords across every store in one dump."""
    var_attr_keys = set()
    group_attr_keys = set()
    coord_names = set()
    grid_enc = set()
    level_enc = set()
    time_enc = set()
    identity_loc = set()
    root_attr_keys = set()
    labels = []
    for store in dump["stores"]:
        labels.append(store["label"])
        var_attr_keys |= _collect_var_attr_keys(store)
        coord_names |= _collect_coord_names(store)
        s = store["signals"]
        grid_enc |= set(s["grid_encoding"])
        level_enc |= set(s["level_encoding"])
        time_enc |= set(s["time_encoding"])
        identity_loc |= set(s.get("identity_location", []))
        group_attr_keys |= set(s.get("common_group_attr_keys", []))
        root_attr_keys |= set(s["root_attr_keys"])
    return {
        "labels": labels,
        "var_attr_keys": var_attr_keys,
        "group_attr_keys": group_attr_keys,
        "coord_names": coord_names,
        "grid_encoding": grid_enc,
        "level_encoding": level_enc,
        "time_encoding": time_enc,
        "identity_location": identity_loc,
        "root_attr_keys": root_attr_keys,
    }


def _print_set_diff(title: str, a_name: str, b_name: str, a: set, b: set) -> None:
    only_a = sorted(a - b)
    only_b = sorted(b - a)
    both = sorted(a & b)
    print(f"\n## {title}")
    print(f"   shared ({len(both)}): {both}")
    print(f"   only in {a_name} ({len(only_a)}): {only_a}")
    print(f"   only in {b_name} ({len(only_b)}): {only_b}")


def diff_dumps(path_a: str, path_b: str) -> int:
    with open(path_a) as f:
        dump_a = json.load(f)
    with open(path_b) as f:
        dump_b = json.load(f)

    sa = _store_summary(dump_a)
    sb = _store_summary(dump_b)
    a_name = dump_a.get("name") or path_a
    b_name = dump_b.get("name") or path_b

    print(f"# Metadata gap analysis")
    print(f"  A = {a_name}: {sa['labels']}")
    print(f"  B = {b_name}: {sb['labels']}")

    _print_set_diff("Grid encoding", a_name, b_name,
                    sa["grid_encoding"], sb["grid_encoding"])
    _print_set_diff("Where identity lives", a_name, b_name,
                    sa["identity_location"], sb["identity_location"])
    _print_set_diff("Level encoding", a_name, b_name,
                    sa["level_encoding"], sb["level_encoding"])
    _print_set_diff("Time encoding", a_name, b_name,
                    sa["time_encoding"], sb["time_encoding"])
    _print_set_diff("Coordinate names", a_name, b_name,
                    sa["coord_names"], sb["coord_names"])
    _print_set_diff("Variable attribute keys", a_name, b_name,
                    sa["var_attr_keys"], sb["var_attr_keys"])
    _print_set_diff("Group attribute keys", a_name, b_name,
                    sa["group_attr_keys"], sb["group_attr_keys"])
    _print_set_diff("Root attribute keys", a_name, b_name,
                    sa["root_attr_keys"], sb["root_attr_keys"])

    print("\n## Takeaway")
    grid_only_a = sorted(sa["grid_encoding"] - sb["grid_encoding"])
    grid_only_b = sorted(sb["grid_encoding"] - sa["grid_encoding"])
    if grid_only_a or grid_only_b:
        print(f"   Grid conventions differ: {a_name} uses {sorted(sa['grid_encoding'])}, "
              f"{b_name} uses {sorted(sb['grid_encoding'])}.")
    else:
        print("   Both sides share the same grid-encoding convention.")
    key_gap = (sa["var_attr_keys"] ^ sb["var_attr_keys"])
    if key_gap:
        print(f"   {len(key_gap)} variable attribute key(s) are present on one side "
              f"but not the other (see 'Variable attribute keys' above).")
    return 0


# --------------------------------------------------------------------------- #
# cli                                                                         #
# --------------------------------------------------------------------------- #

def _run_inventory(args) -> int:
    stores = []
    for path in args.local or []:
        print(f"Opening local {path}", file=sys.stderr)
        dt = open_local(path, args.branch)
        stores.append(inventory_store(dt, label=path))
    for prefix in args.prefix or []:
        print(f"Opening s3://{args.bucket}/{prefix}", file=sys.stderr)
        dt = open_s3(args.bucket, prefix, args.region, args.branch)
        stores.append(inventory_store(dt, label=prefix))

    if not stores:
        print("No stores given. Use --local and/or --prefix.", file=sys.stderr)
        return 2

    dump = {"name": args.name, "stores": stores}
    text = json.dumps(dump, indent=2, sort_keys=True)
    if args.out:
        with open(args.out, "w") as f:
            f.write(text)
        print(f"Wrote {args.out} ({len(stores)} store(s))", file=sys.stderr)
    else:
        print(text)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    inv = sub.add_parser("inventory", help="Dump one or more stores' metadata to JSON.")
    inv.add_argument("--local", action="append", help="Local store path (repeatable).")
    inv.add_argument("--prefix", action="append", help="S3 store prefix (repeatable).")
    inv.add_argument("--bucket", default="wsupviewer-eks-transfer")
    inv.add_argument("--region", default="us-east-1")
    inv.add_argument("--branch", default="main")
    inv.add_argument("--name", default=None, help="Label for this dump (e.g. 'cirrus').")
    inv.add_argument("--out", default=None, help="Write JSON here instead of stdout.")

    df = sub.add_parser("diff", help="Compare two inventory dumps.")
    df.add_argument("a", help="First inventory JSON (e.g. cirrus).")
    df.add_argument("b", help="Second inventory JSON (e.g. nwsviz).")

    args = p.parse_args(argv)
    if args.cmd == "inventory":
        return _run_inventory(args)
    if args.cmd == "diff":
        return diff_dumps(args.a, args.b)
    return 2


if __name__ == "__main__":
    sys.exit(main())
