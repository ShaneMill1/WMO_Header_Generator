"""Quick-and-dirty inspector for a local-filesystem icechunk store.

Opens an icechunk repo on disk (read-only) and prints each group as a plain
xarray ``Dataset`` (xarray's native repr). No registry / WMO resolution -- just
a look at what's actually in the store.

Usage::

    python inspect_icechunk.py /efs/cirrus_test_data/KDFX_icechunk
    python inspect_icechunk.py /efs/cirrus_test_data/KTLX_167_0.5_icechunk --branch main
    python inspect_icechunk.py <path> --group /sweep_0   # just one group
"""

from __future__ import annotations

import argparse
import sys
import warnings

import icechunk
import xarray as xr

warnings.filterwarnings("ignore")


def open_tree(path: str, branch: str) -> xr.DataTree:
    storage = icechunk.local_filesystem_storage(path)
    repo = icechunk.Repository.open(storage)
    return xr.open_datatree(
        repo.readonly_session(branch).store,
        engine="zarr",
        chunks=None,
        consolidated=False,
    )


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("path", help="Path to the local icechunk store directory.")
    p.add_argument("--branch", default="main")
    p.add_argument("--group", default=None,
                   help="Only show this group (e.g. /sweep_0). Default: all.")
    args = p.parse_args(argv)

    print(f"Opening {args.path} (branch={args.branch})\n")
    dt = open_tree(args.path, args.branch)

    groups = [args.group] if args.group else sorted(dt.groups)

    for gpath in groups:
        ds: xr.Dataset = dt[gpath].to_dataset()
        print(f"===== group: {gpath} =====")
        print(ds)
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
