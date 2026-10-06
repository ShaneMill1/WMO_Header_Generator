"""Survey coverage across one icechunk store per model in the bucket.

For each store it resolves every product (latest time, first level) and reports
how many headings resolved versus were left UNRESOLVED, with a representative
reason. This is a coverage probe, not the product tool -- use read_icechunk.py
for a single store.
"""

from __future__ import annotations

import sys
import warnings

import icechunk
import numpy as np
import xarray as xr

from grib_identity import extract_identity, message_pdt
from wmo_header import Unresolved, WMOHeader, load_registry, resolve

warnings.filterwarnings("ignore")

BUCKET = "wsupviewer-eks-transfer"

# One representative .ic per top-level dataset (first sub-prefix, first store).
STORES = {
    "air_quality": "edr-api/air_quality/2025-11-06T06:00/air_quality-conus_lambert-2025-11-06T06:00.ic",
    "gefs0p25_v12p3": "edr-api/gefs0p25_v12p3/2021-01-01T00:00/gefs0p25_v12p3-global_latlon-2021-01-01T00:00.ic",
    "gefs0p5_v12p3": "edr-api/gefs0p5_v12p3/2021-01-01T00:00/gefs0p5_v12p3-global_latlon-2021-01-01T00:00.ic",
    "gfs0p25_v16p3": "edr-api/gfs0p25_v16p3/2021-01-01T00:00/gfs0p25_v16p3-global_latlon-2021-01-01T00:00.ic",
    "gfs0p25_v17": "edr-api/gfs0p25_v17/2025-12-16T00:00/gfs0p25_v17-global_latlon-2025-12-16T00:00.ic",
    "mrms_v12p2": "edr-api/mrms_v12p2/2021-01/mrms_v12p2-conus_lambert-2021-01.ic",
    "processed_gefs0p25_v12p3": "edr-api/processed_gefs0p25_v12p3/2021-01-01T00:00/processed_gefs0p25_v12p3-global_latlon-2021-01-01T00:00.ic",
    "rtma2p5_v2p10": "edr-api/rtma2p5_v2p10/2017-05/rtma2p5_v2p10-conus_lambert-2017-05.ic",
    # rtma_climo_mean_2016_2026 omitted: its only prefix (latest/) is an empty
    # icechunk repo with no data at the time of this survey.
    "schnapp_unet_v0": "edr-api/schnapp_unet_v0/2022-01-01T00:00/schnapp_unet_v0-conus_lambert-2022-01-01T00:00.ic",
    "urma2p5_v2p10": "edr-api/urma2p5_v2p10/2025-12/urma2p5_v2p10-conus_lambert-2025-12.ic",
}

DOMAIN_KEYWORDS = {
    "conus": "CONUS", "alaska": "Alaska", "hawaii": "Hawaii",
    "guam": "Guam", "caribbean": "Caribbean",
}


def infer_domain(text: str):
    low = text.lower()
    for kw, label in DOMAIN_KEYWORDS.items():
        if kw in low:
            return label
    return None


def open_store(prefix: str):
    storage = icechunk.s3_storage(
        bucket=BUCKET, prefix=prefix, region="us-east-1", from_env=True
    )
    return xr.open_datatree(
        icechunk.Repository.open(storage).readonly_session("main").store,
        engine="zarr", chunks=None, consolidated=False,
    )


def survey_one(name: str, prefix: str, registry) -> dict:
    from read_icechunk import infer_dataset_key
    domain = infer_domain(prefix)
    dataset_key = infer_dataset_key(prefix)
    try:
        dt = open_store(prefix)
    except Exception as e:  # noqa: BLE001 -- report, don't abort the survey
        return {"name": name, "error": f"{type(e).__name__}: {e}"}

    exact = 0
    param = 0
    total = 0
    seen_products: set = set()
    sample_ok = None
    reasons: dict = {}

    for gpath in sorted(dt.groups):
        ds = dt[gpath].to_dataset()
        for var_name, da in ds.data_vars.items():
            ident = extract_identity(da, ds, registry)
            if ident.short_name in seen_products:
                continue
            seen_products.add(ident.short_name)
            total += 1

            # A product resolves if ANY of its (time, level) messages resolves;
            # sampling only the last one under-reports, since forecast-hour and
            # accumulation windows vary per message. Probe a representative set:
            # every level, and a spread of forecast times.
            times = ident.times or [None]
            t_probe = times if len(times) <= 4 else [
                times[0], times[len(times) // 3], times[2 * len(times) // 3], times[-1]
            ]
            levels = ident.levels or [None]
            best = None
            last_unres = None
            for t in t_probe:
                lead = None
                if ident.lead_hours and t is not None and t in ident.times:
                    lead = ident.lead_hours[ident.times.index(t)]
                for lvl in levels:
                    r = resolve(
                        ident.short_name, reference_time=t, registry=registry,
                        domain=domain, dataset_key=dataset_key,
                        identity={"section3": ident.section3, "pdtn": ident.pdtn,
                                  "pdt": message_pdt(ident, lvl, lead),
                                  "model_cycle_hour": ident.model_cycle_hour},
                    )
                    if isinstance(r, WMOHeader):
                        best = r
                        break
                    last_unres = r
                if best is not None:
                    break

            if best is not None:
                if best.match == "exact":
                    exact += 1
                else:
                    param += 1
                if sample_ok is None:
                    tag = "" if best.match == "exact" else " [param-level]"
                    sample_ok = f"{ident.short_name} -> {best.heading}{tag}"
            else:
                key = _reason_key(last_unres)
                reasons[key] = reasons.get(key, 0) + 1

    return {
        "name": name, "domain": domain, "products": total,
        "exact": exact, "param": param, "unresolved": total - exact - param,
        "sample": sample_ok, "reasons": reasons,
    }


def _reason_key(r: Unresolved) -> str:
    """Collapse a reason list to a short bucket for tallying.

    The identity-match reason is the substantive one for model data (the
    name-match reason is just 'not in an MRMS-style notice', which is expected
    for gridded model fields), so it is checked first.
    """
    joined = " ".join(r.reasons).lower()
    if "no assigned grid carries a wmo header" in joined:
        return "parameter not headered on any assigned grid"
    if "different ensemble" in joined or "ensemble" in joined:
        return "ensemble/derived product (different template)"
    if "matches" in joined and "different headers" in joined:
        return "identity underspecified (multiple headers)"
    if "reference time" in joined:
        return "no usable reference time"
    if "no wmo header assignment" in joined:
        return "grid not assigned a header"
    if "abbreviation" in joined:
        return "parameter abbreviation unknown to registry"
    if "not among those nws disseminates" in joined:
        return "parameter never disseminated with a header"
    if "no authoritative nws notice" in joined:
        return "not a named-notice product"
    return r.reasons[0][:70] if r.reasons else "unknown"


def main() -> int:
    registry = load_registry()
    print(f"Registry: {registry.describe()}\n")
    print(f"{'model':26s} {'prod':>5s} {'exact':>6s} {'param':>6s} {'unres':>6s}  sample / reasons")
    print("-" * 104)

    for name, prefix in STORES.items():
        r = survey_one(name, prefix, registry)
        if "error" in r:
            print(f"{name:26s} ERROR: {r['error']}")
            continue
        print(
            f"{r['name']:26s} {r['products']:5d} {r['exact']:6d} {r['param']:6d} "
            f"{r['unresolved']:6d}  {r['sample'] or ''}"
        )
        for reason, count in sorted(r["reasons"].items(), key=lambda kv: -kv[1]):
            print(f"{'':46s}  {count:4d}x {reason}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
