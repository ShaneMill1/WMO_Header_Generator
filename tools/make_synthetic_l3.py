"""Build a synthetic CF/Radial Level III icechunk store for development.

Why this exists: the two real radar samples (KDFX Level II, KTLX product 167)
both correctly resolve to *no* WMO heading -- L2 has none, and 167 (Super Res
Digital Correlation Coefficient) is defined but not SBN-disseminated. Neither can
exercise the resolve-to-a-real-heading path.

This creates a minimal store that mimics a *disseminated* L3 product so that path
can be developed and tested. It copies the exact schema of the real KTLX 167
store (same dims, coords, attrs) but sets:

    product_code = 165   (Digital Hydrometeor Classification -- SDUS8i on the SBN)
    moment_name  = "DHC"
    elevation_angle = 0.5   (-> NNN mnemonic N0H in the NOAAPort table)

It is throwaway test data, not authoritative radar output: the array values are
synthetic. Only the *structure and identifying metadata* matter for resolver
development.

Usage::

    python make_synthetic_l3.py                       # default output path
    python make_synthetic_l3.py --out /path/to.ic
"""

from __future__ import annotations

import argparse
import sys
import warnings

import icechunk
import numpy as np
import xarray as xr

warnings.filterwarnings("ignore")

DEFAULT_OUT = "/efs/cirrus_test_data/KTLX_165_0.5_icechunk"
GROUP = "/KTLX/165_DHC"


def build_dataset() -> xr.Dataset:
    """A single-sweep DHC product shaped exactly like the real KTLX 167 store."""
    n_ray, n_gate, n_pdv = 720, 1200, 10
    rng = np.random.default_rng(165)

    azimuth = np.linspace(0.0, 360.0, n_ray, endpoint=False, dtype="float32")
    rng_m = (np.arange(n_gate, dtype="float32") * 250.0)
    # Hydrometeor classification categories are small integers; store as float
    # like the real moment does.
    hc = rng.integers(0, 15, size=(1, n_ray, n_gate)).astype("float32")

    ds = xr.Dataset(
        data_vars={
            "DHC": (
                ("time", "ray", "gate"),
                hc,
                {
                    "long_name": "Digital Hydrometeor Classification",
                    "units": "category",
                    "valid_min": 0.0,
                    "valid_max": 150.0,
                },
            ),
        },
        coords={
            "time": (("time",), np.array(["2026-01-08T16:17:14"], dtype="datetime64[ns]")),
            "elevation_angle": (("time",), np.array([0.5], dtype="float64")),
            "azimuth": (("time", "ray"), azimuth[None, :]),
            "range": (("time", "gate"), rng_m[None, :]),
            "product_dependent_values": (
                ("time", "pdv"),
                np.zeros((1, n_pdv), dtype="int64"),
            ),
            "quality_control_flags_json": (("time",), np.array(["{}"], dtype=object)),
        },
        attrs={
            "product_code": 165,
            "product_name": "Digital Hydrometeor Classification",
            "moment_name": "DHC",
            "site_id": "KTLX",
            "latitude": 35.333,
            "longitude": -97.278,
            "altitude": 1277.0,
            "vcp_number": 212,
            "scan_time": "2026-01-08T16:17:14+00:00",
            "Conventions": "CF/Radial",
        },
    )
    return ds


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", default=DEFAULT_OUT, help="Output icechunk store path.")
    args = p.parse_args(argv)

    ds = build_dataset()
    dt = xr.DataTree.from_dict({GROUP: ds})

    storage = icechunk.local_filesystem_storage(args.out)
    repo = icechunk.Repository.open_or_create(storage)
    session = repo.writable_session("main")
    dt.to_zarr(session.store, mode="w", consolidated=False)
    session.commit("synthetic L3 DHC (product 165) for resolver development")

    print(f"Wrote synthetic L3 store: {args.out}")
    print(f"  group={GROUP} product_code=165 moment=DHC elevation=0.5")
    return 0


if __name__ == "__main__":
    sys.exit(main())
