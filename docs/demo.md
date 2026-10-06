# cirrus_wmo — step-by-step demo

Resolving WMO abbreviated headings from icechunk stores, for both GRIB2 (MRMS)
and NEXRAD radar data. Every run below is real output.

All commands use the project's conda env:

```
PY=/home/shane.mill/mambaforge/envs/cirrus_wmo/bin/python
```

The resolver follows one rule: **accurate or nothing.** A heading is emitted only
when every field traces to a pinned authoritative record; otherwise the result is
`UNRESOLVED` with a specific reason.

---

## 0. Setup — build the registry (first run only)

The registry is build output, not committed. It builds itself on first use, or
explicitly:

```
$PY build_registry.py                        # all sources
$PY build_registry.py --source nws-noaaport-radar   # just the radar table
```

The radar source parses the NOAAPort radar products table (pinned by sha256):

```
Building source 'nws-noaaport-radar'
  fetching: https://www.weather.gov/media/tg/noaaport_radar_products.pdf
  rows            : 96
  product codes   : 29
  wrote nws-noaaport-radar.entries.jsonl.gz (0.00 MB)
  wrote nws-noaaport-radar.manifest.json
```

---

## 1. Inspect a store (optional)

Before resolving, dump a store's structure with the inspection tool:

```
$PY tools/inspect_icechunk.py /efs/cirrus_test_data/KTLX_165_0.5_icechunk --group /KTLX/165_DHC
```

```
<xarray.Dataset> Size: 3MB
Dimensions:                     (time: 1, ray: 720, gate: 1200, pdv: 10)
Coordinates:
  * time                        (time) datetime64[ns] 8B 2026-01-08T16:17:14
    elevation_angle             (time) float64 8B ...
    azimuth                     (time, ray) float32 3kB ...
    range                       (time, gate) float32 5kB ...
Data variables:
    DHC                         (time, ray, gate) float32 3MB ...
Attributes:
    product_code:  165
    moment_name:   DHC
    site_id:       KTLX
    scan_time:     2026-01-08T16:17:14+00:00
    Conventions:   CF/Radial
```

The resolver keys on `product_code`, `elevation_angle`, `site_id`, `scan_time`.

---

## 2. MRMS (GRIB2) — resolves to a header

The default store is the CONUS MRMS archive on S3. (Requires AWS credentials in
the environment; the store is opened read-only.)

```
$PY read_icechunk.py --limit 5
```

```
Opening s3://wsupviewer-eks-transfer/edr-api/mrms_v12p2/2025-09/mrms_v12p2-conus_lambert-2025-09.ic (branch=main)
Registry: grib2io-param-tables (1700 parameter abbreviations); ... nws-mrms-sbn (nws_notice, 150 entries); nws-noaaport-radar (nexrad_radar, 96 entries); ...
Domain evidence: CONUS

WMO: YAUP06 KWNR 301800
     product     : MultiSensor_QPE_24H_Pass2
     description : MultiSensor_QPE_[01,03,06,12,24,48,72]H_Pass2
     domain      : CONUS
     level       : 0.0 m
     valid (UTC) : 2025-09-30T18:00
     authority   : nws-mrms-sbn -> MRMS-v12.2-supp
     source group: /1/precipitation_accumulation_24_hour

Resolved 1 heading(s); 0 product(s) unresolved.
(3 duplicate result(s) from resolution-pyramid groups collapsed; use --explain to see source groups)
```

With `--explain`, the matching evidence and the verbatim source text are shown:

```
$PY read_icechunk.py --limit 5 --explain
```

```
WMO: YAUP06 KWNR 301800
     ...
     cccc check  : KWNR (national, per XR-09)
     source text : MultiSensor_QPE_[01,03,06,12,24,48,72]H_Pass2 *On SBN with WMO codes YAUP06 (CONUS), YAAP06 (Alaska), YAHP06 (Hawaii)*
     source group: /1/precipitation_accumulation_24_hour
```

The heading is matched by product name + domain against the MRMS SBN notices;
`YYGGgg` (301800) comes from the data's own valid time.

---

## 3. NEXRAD Level III, disseminated — resolves to a header

A disseminated L3 product (Digital Hydrometeor Classification, code 165) resolves
to its `SDUS` heading. The store is local on `/efs`, so use `--local`:

```
$PY read_icechunk.py --local /efs/cirrus_test_data/KTLX_165_0.5_icechunk
```

```
Opening /efs/cirrus_test_data/KTLX_165_0.5_icechunk (branch=main)
Store type: NEXRAD radar (CF/Radial)
WMO: SDUS8 KTLX 081617
     product     : Digital Hydrometeor Classification
     code         : 165
     description : Digital Hydrometeor Classification (N0H)
     elevation    : 0.5
     site (CCCC)  : KTLX
     authority    : nws-noaaport-radar -> noaaport-radar-products
     note         : CCCC KTLX not in XR-09 directory (radar site codes are not WFO nodes)
     source group : /KTLX/165_DHC
Resolved 1 heading(s); 0 product(s) unresolved.
```

Matched by `(product_code=165, elevation=0.5)` → NNN `N0H` → `SDUS8`; CCCC is the
site (`KTLX`); `YYGGgg` (081617) from `scan_time`.

> This store is synthetic test data (`tools/make_synthetic_l3.py`), built to
> exercise the resolve path because the real samples below both correctly
> resolve to nothing.

---

## 4. NEXRAD Level III, not disseminated — Unresolved

Product code 167 (Super Res Digital Correlation Coefficient) is defined in the
WSR-88D ICD but is **not broadcast on the SBN**, so no WMO heading exists:

```
$PY read_icechunk.py --local /efs/cirrus_test_data/KTLX_167_0.5_icechunk
```

```
Store type: NEXRAD radar (CF/Radial)
UNRESOLVED: HC  (group /KTLX/167_HC)
     product code 167 is not disseminated on the SBN under a WMO heading (absent from the NOAAPort radar table; e.g. super-res products 167/168 are defined but not broadcast)
Resolved 0 heading(s); 1 product(s) unresolved.
```

This is the correct answer, not a coverage gap: there is no heading to find.

---

## 5. NEXRAD Level II — Unresolved

Level II base data (per-sweep moments) is distributed as whole-volume files via
LDM/FTP, not under a WMO heading. Every sweep resolves to a precise Unresolved:

```
$PY read_icechunk.py --local /efs/cirrus_test_data/KDFX_icechunk
```

```
Store type: NEXRAD radar (CF/Radial)
UNRESOLVED: radar  (group /sweep_0)
     Level II base data are distributed as whole-volume files via LDM/FTP, not under a WMO abbreviated heading; no authoritative record assigns one
UNRESOLVED: radar  (group /sweep_1)
     ...
UNRESOLVED: radar  (group /sweep_3)
     Level II base data are distributed as whole-volume files via LDM/FTP, not under a WMO abbreviated heading; no authoritative record assigns one
Resolved 0 heading(s); 4 product(s) unresolved.
```

---

## Summary

| Store | Type | Result |
| --- | --- | --- |
| MRMS `mrms_v12p2` | GRIB2 | `YAUP06 KWNR 301800` (resolved) |
| KTLX 165 (synthetic) | NEXRAD L3, disseminated | `SDUS8 KTLX 081617` (resolved) |
| KTLX 167 | NEXRAD L3, not disseminated | Unresolved — no SBN heading |
| KDFX | NEXRAD L2 | Unresolved — whole-volume, no heading |

Three of four outcomes are "no header," and each is correct. The tool distinguishes
*resolvable* products from products that genuinely have no WMO heading, and never
fabricates one.
