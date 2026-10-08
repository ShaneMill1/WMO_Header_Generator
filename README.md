# cirrus_wmo: WMO abbreviated heading resolver

Repository: https://github.com/ShaneMill1/WMO_Header_Generator

## Overview

Given a weather dataset in storage, `cirrus_wmo` reports the WMO abbreviated
heading it would be disseminated under, or, if it would not be disseminated, the
specific reason why. The heading cannot be computed from the data; it is a human
allocation recorded in an authoritative document. The tool resolves it by
matching the dataset's identity against those documents.

```
$ python read_icechunk.py
WMO: YAUP06 KWNR 301800
     product     : MultiSensor_QPE_24H_Pass2
     domain      : CONUS
     valid (UTC) : 2025-09-30T18:00
     authority   : nws-mrms-sbn -> MRMS-v12.2-supp
```

Supported products: model output (GFS, GEFS, AQM, HRRR), MRMS, RTMA/URMA, and
NEXRAD radar.

## The heading

```
YAUP06   KWNR   301800   (BBB)
  │       │       │        └─ optional correction/amendment indicator
  │       │       └─ day 30, 18:00 UTC (the only field derived from the data)
  │       └─ originating center (KWNR)
  └─ product + region code (YAUP06)
```

| Term | Meaning |
| --- | --- |
| **WMO heading** | `TTAAII CCCC YYGGgg` |
| **TTAAII** | product/region designator (e.g. `YAUP06`) |
| **CCCC** | originating center (e.g. `KWNR`, `KWBC`) |
| **YYGGgg** | day/hour/minute, derived from the data's timestamp |
| **disseminated** | broadcast on the network; a product that isn't has no heading |
| **GRIB2** | standard format for gridded model/analysis data |
| **NEXRAD** | national weather-radar network |
| **icechunk / zarr** | storage format the datasets live in |
| **registry** | this tool's local database of product → heading mappings |

## Architecture

![architecture](docs/architecture.png)

1. **Input**: a dataset in storage (an icechunk / zarr store).
2. **Identify**: read the dataset's metadata into a product identity. Model data
   and radar describe themselves differently, so each has its own reader.
3. **Registry**: a local SQLite database (`registry.db`) built from pinned NWS /
   NCEP / WMO documents that record which product gets which heading. Each
   document is pinned to an exact version and checksum (see "Registry database").
4. **Resolve**: match the identity against those sources.
5. **Result**: a heading traced to a record, or an `Unresolved` with a reason.

## Mapping

Resolution is a lookup, not a computation. A heading's
[fields](https://www.weather.gov/tg/headef) `TTAAII` and `CCCC` are a
human allocation decision, made once when a product is added to dissemination and
recorded in an operational config or public notice; they are not derivable from
the data. (The `ii` of `TTAAII`, for instance, is a two-digit designator assigned
from the WMO/national-practice tables.) Even the operational tool (`tocgrib2`)
does not compute headings; it looks them up in pre-written records and copies the
string out, stamping only `YYGGgg`. This project runs that lookup in reverse:
given archived metadata, find the allocation already made, and where none exists,
report that there is no heading to find.

Different products are identified by different attributes (radar by site and
tilt, a model field by grid and variable), so there is no single lookup key. Each
source is matched on the fields it actually uses. The invariant across all of
them: **`TTAAII` and `CCCC` come from the source, `YYGGgg` comes from the data.**
Radar is the one exception; its `CCCC` is the radar site, read from the data.

| Source | Fields read from the store | Matched against | Produces the header by |
| --- | --- | --- | --- |
| **MRMS** (SBN notices) | product `short_name`; domain (from the store path, e.g. `…conus…`) | normalized product name + domain, in the notice entries | copying `code` → `TTAAII` and `cccc` → `CCCC` straight from the matched entry |
| **RTMA / URMA** (TINs) | parameter `short_name`; `dataset\|resolution\|domain` key (from the store path, e.g. `rtma\|2p5\|conus`) | parameter → `T2` lookup, and dataset key → `A1` lookup, in the TIN tables | **composing** `TTAAII = T1 + T2 + A1 + A2ii` (T1/A2ii/CCCC are fixed per table) |
| **GFS / GEFS / AQM** (tocgrib2 parm) | `grib_section3` (grid), `short_name` (→ parameter), PDT fields: level, forecast hour, generating process, statistical window | grid match + parameter + every PDT field the parm record pins (wildcards where the record leaves `-9999`) | copying `TTAAII` / `CCCC` from the matched parm record |
| **HRRR** (tocgrib2 parm) | same identity, but read from **CF metadata**: grid from the `spatial_ref` grid-mapping + `x`/`y`; level from the scaled-value coords; forecast hour from `lead_time`; reference time from `init_time` | parameter + level + forecast hour, scoped to HRRR's own records (`KWBY`) | copying `TTAAII` / `CCCC` from the matched HRRR parm record |
| **NEXRAD L3** (NOAAPort table) | `product_code`, `elevation_angle`, `site_id`, `scan_time` | `(product_code, elevation)` → one table row → mnemonic + `SDUS` tier | `TTAAII` = the row's `SDUS<tier>`; `CCCC` = `site_id` from the data |

Two details worth calling out:

- **The parameter is never stored, so it's inferred.** GRIB2 archives keep the
  product's `short_name` but not its discipline/category/number. Those are looked
  up from `short_name` against the grib2io parameter tables, then matched against
  the parm record. If a name maps to more than one parameter, that's reported as
  ambiguous rather than guessed (this is why some GFS/GEFS fields like `CFRZR`
  come back unresolved).
- **Exact vs parameter-level (GRIB2).** If the store's grid *is* a disseminated
  grid, the match is **exact**: the header is authoritative for that data. If the
  store is on a native, non-disseminated grid (e.g. GFS at 0.25°, HRRR at 3 km),
  the resolver reports the header NWS assigns to the *same field on its AWIPS
  grid*, flagged as **parameter-level** with a caveat, never as if it were the
  real bulletin.
- **Two GRIB2 metadata encodings.** The same GRIB2 identity can be stored two
  ways. Some archives keep the raw GRIB2 Section 3 array (`grib_section3`) and
  PDT prose; others (e.g. the CIRRUS HRRR stores) keep it as CF metadata: a
  `spatial_ref` grid-mapping, projected `x`/`y`, and scaled-value level
  coordinates. The resolver reads either; the join to the authoritative source is
  identical once the grid, parameter, level, and forecast hour are in hand.

### Worked example (MRMS)

![how a header is resolved](docs/resolve_example.png)

The dataset's metadata (left) is matched to one row in the MRMS source table
(middle); the heading fields on that row (highlighted) are copied into the
final heading (right), with only the timestamp filled in from the data. Step by
step, for the MRMS example at the top of this README:

1. **Read the identity from the store.** The variable's metadata gives
   `short_name = "MultiSensor_QPE_24H_Pass2"` and `time = 2025-09-30T18:00`; the
   store path `…conus_lambert…` gives `domain = CONUS`.

2. **Find the matching source row.** At build time the MRMS SBN notice was parsed
   into rows and indexed by product name. The identity's name + domain select one
   row:

   ```
   product_names : ["MultiSensor_QPE_24H_Pass2", …]
   domain        : "CONUS"
   code          : "YAUP06"     ← TTAAII, written by a human in the notice
   cccc          : "KWNR"       ← originating office, from the notice
   ```

3. **Read the header off the row, stamp the time from the data.**
   `TTAAII = code = YAUP06`, `CCCC = cccc = KWNR` (both copied from the row), and
   `YYGGgg = 301800` (the day/hour/minute computed from `2025-09-30T18:00`).

   Result: **`YAUP06 KWNR 301800`.**

The other sources differ only in the join key, and for RTMA/URMA the heading is
assembled from table pieces rather than copied whole; otherwise the flow is the
same.

## Accurate or nothing

Every `TTAAII`/`CCCC` the resolver emits traces to an authoritative record; the
tool never fabricates one. It does not default a domain or originating center,
invent a timestamp, or choose between candidates the evidence cannot distinguish.
When nothing traces, the result is an `Unresolved` stating what was missing, and
an unresolved result is often the correct answer.

The one qualified case is grid: for a GRIB2 store on a native, non-disseminated
grid, the resolver reports the header NWS assigns to the *same field on its
disseminated grid*, flagged as **parameter-level** with a caveat. The codes are
still copied from a real record; what the caveat makes explicit is that they
identify the field, not a bulletin for this exact grid (there is none). This is
reported, never silent, and it is distinct from an exact match. See the Demo for
which products resolve exactly, which resolve with a caveat, and which do not.

## Demo

All output below is real. Commands use the project's Python environment.

### MRMS (GRIB2): resolves

```
$ python read_icechunk.py --prefix "edr-api/mrms_v12p2/2025-09/mrms_v12p2-conus_lambert-2025-09.ic" --limit 5
WMO: YAUP06 KWNR 301800
     product     : MultiSensor_QPE_24H_Pass2
     description : MultiSensor_QPE_[01,03,06,12,24,48,72]H_Pass2
     domain      : CONUS
     valid (UTC) : 2025-09-30T18:00
     authority   : nws-mrms-sbn -> MRMS-v12.2-supp
     source group: /1/precipitation_accumulation_24_hour
Resolved 1 heading(s); 0 product(s) unresolved.
```

This MRMS prefix is also the built-in default, so running `read_icechunk.py` with
no arguments resolves the same store. Add `--explain` to see the matching evidence
and the verbatim source text.

### RTMA / URMA (GRIB2 analysis): resolves

Analysis-grid fields that the NWS TINs assign a header to resolve; fields the
TINs don't cover (cloud ceiling, specific humidity, …) correctly do not.

```
$ python read_icechunk.py --prefix "edr-api/rtma2p5_v2p10/2018-01/rtma2p5_v2p10-conus_lambert-2018-01.ic" --limit 5
WMO: LTIA98 KWBR 312300
     product     : TMP
     description : temperature analysis
     level       : 2.0 m
     valid (UTC) : 2018-01-31T23:00
     authority   : nws-rtma-urma -> TIN11-42 + TIN11-42
     source group: /1/temperature_height_above_ground
...
Resolved 5 heading(s); 3 product(s) unresolved.
Unresolved products: ['CEIL', 'SPFH', 'TCDC']
```

(URMA behaves the same, e.g. `LTQA98 KWBR` for temperature.)

### GFS at native 0.25° (GRIB2): parameter-level, with a caveat

The archived GFS is on its native global grid, which is not disseminated, so the
result is a parameter-level match (see the `note` field):

```
$ python read_icechunk.py --prefix "edr-api/gfs0p25_v16p3/2021-01-01T00:00/gfs0p25_v16p3-global_latlon-2021-01-01T00:00.ic" --limit 5
WMO: YHPY99 KWBC 110000
     product     : HGT
     description : HGT      1000 mb
     match       : parameter-level (see note)
     note        : NWS assigns this to the field on grid 'gfs_awips_1p0deg_003';
                   the archive is the native file grid (1440x721 gdt0), which is
                   not disseminated under a WMO header
     authority   : ncep-gfs-awips -> grib2_awpgfs240.003:1
```

### HRRR, CF-encoded (GRIB2): parameter-level, with a caveat

A CIRRUS HRRR store carries its GRIB2 identity as CF metadata, not a
`grib_section3` array. The store type and model are detected automatically, and
the field resolves to HRRR's own `KWBY` header. As with GFS, the native grid
(here 3 km, not the disseminated 2.5 km grid 184) makes it parameter-level:

```
$ python read_icechunk.py --local /efs/cirrus_test_data/HRRR-2026-09-21-sample-v2.icechunk --limit 1
Store type: CF-encoded GRIB2 (model source: ncep-hrrr-awips)
WMO: YHCA73 KWBY 160700
     product     : HGT
     description : HGT      Cloud base lvl
     match       : parameter-level (see note)
     note        : NWS assigns this to the field on grid 'hrrr_awips_184_2p5km_lambert';
                   the archive is the native file grid (1799x1059 gdt30), which is
                   not disseminated under a WMO header
     lead (h)    : 0.0
     authority   : ncep-hrrr-awips -> grib2_awips_hrrrf00.184:56
     source group: /cloud_ceiling/HGT
```

The forecast hour drives the `A2` character of the heading, so the same field
across lead times produces `YHCA…`, `YHCB…`, `YHCC…` (f00, f01, f02, …), each
tracing to the matching per-hour parm file.

### NEXRAD Level III, disseminated: resolves

A radar store is detected automatically; local stores use `--local`.

```
$ python read_icechunk.py --local /efs/cirrus_test_data/KTLX_165_0.5_icechunk
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

Matched by `(product code 165, elevation 0.5°)` → mnemonic `N0H` → `SDUS8`;
site `KTLX` becomes the CCCC; the time comes from the scan time.

### NEXRAD Level III, not disseminated: unresolved

Product 167 (Super-Res Digital Correlation Coefficient) is a real product but is
not broadcast on the SBN, so it has no heading:

```
$ python read_icechunk.py --local /efs/cirrus_test_data/KTLX_167_0.5_icechunk
UNRESOLVED: HC  (group /KTLX/167_HC)
     product code 167 is not disseminated on the SBN under a WMO heading
     (absent from the NOAAPort radar table; e.g. super-res products 167/168
     are defined but not broadcast)
Resolved 0 heading(s); 1 product(s) unresolved.
```

### NEXRAD Level II: unresolved

Level II base data moves as whole-volume files (LDM/FTP), not as headered
bulletins, so each sweep resolves to a precise reason:

```
$ python read_icechunk.py --local /efs/cirrus_test_data/KDFX_icechunk
UNRESOLVED: radar  (group /sweep_0)
     Level II base data are distributed as whole-volume files via LDM/FTP,
     not under a WMO abbreviated heading; no authoritative record assigns one
...
Resolved 0 heading(s); 4 product(s) unresolved.
```

### Summary of results

| Product | Result | Why |
| --- | --- | --- |
| MRMS SBN product (e.g. 24-hour QPE) | ✅ exact, `YAUP06 KWNR 301800` | assigned a header in the MRMS SBN notices |
| RTMA / URMA analysis field in the TINs | ✅ exact, `LTIA98 KWBR 312300` | header pieces published in the NWS TINs |
| NEXRAD Level III SBN product (e.g. 165 DHC) | ✅ exact, `SDUS8 KTLX 081617` | listed in the NOAAPort radar table |
| GFS / GEFS at native 0.25° resolution | ⚠️ parameter-level, `YHPY99 KWBC 110000` | the native grid isn't disseminated, so the header for the same field on the AWIPS grid is reported with a caveat (only where that field is headered; otherwise unresolved) |
| HRRR at native 3 km (CF-encoded) | ⚠️ parameter-level, `YHCA73 KWBY 160700` | same as GFS: native grid isn't the disseminated 2.5 km grid, so HRRR's own `KWBY` header is reported with a caveat |
| RTMA / URMA field not in the TINs (CEIL, SPFH, TCDC) | ❌ | no NWS record assigns it a header |
| NEXRAD Level III, not on the SBN (e.g. 167, 168) | ❌ | real product, but never broadcast under a heading |
| NEXRAD Level II base data | ❌ | distributed as whole-volume files (LDM/FTP), never headered |

Three result types: an **exact** heading (authoritative for that data), a
**parameter-level** heading (the field is headered on a different, disseminated
grid, reported with a caveat), or **unresolved** with a reason. A ❌ reflects a
product with no WMO heading to find, not a gap in coverage.

## Installation

Python 3.13 is recommended (the project is developed and tested against it).
Create an environment and install the runtime dependencies:

```bash
# venv + pip
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# or conda / mamba
mamba create -n cirrus_wmo python=3.13
mamba activate cirrus_wmo
pip install -r requirements.txt
```

That installs `icechunk`, `xarray`, `zarr`, `numpy`, and `pypdf` at the versions
the project is tested against. To run the test suite as well, add the dev
dependencies:

```bash
pip install -r requirements-dev.txt   # adds pytest
```

`grib2io` is deliberately not a dependency: `build_registry.py` downloads
grib2io's source tarball from a pinned URL and parses its parameter tables
directly, so there is nothing extra to install for the registry build. Reading
S3 stores needs AWS credentials in the environment (`icechunk` handles the S3
access); local stores via `--local` need none.

## Usage

```bash
# The registry builds itself on first use.
python read_icechunk.py                          # default MRMS store (S3)
python read_icechunk.py --prefix path/to/store.ic --explain
python read_icechunk.py --local /path/to/radar_store   # local radar store
python read_icechunk.py --local /path/to/hrrr.icechunk # CF-encoded GRIB2 (HRRR)
#   model source is inferred from the path; override with --source <id>

# Rebuild the registry explicitly (e.g. after editing sources.json).
python build_registry.py
python build_registry.py --source ncep-hrrr-awips

python -m pytest tests/ -q                       # hermetic tests, no network
```

The default MRMS store is on S3 and needs AWS credentials in the environment;
local stores (e.g. radar on `/efs`) use `--local` and need none.

## Sources

The registry is built from pinned, hash-verified public documents, each covering
a different piece of the mapping:

| Source | What it provides |
| --- | --- |
| **tocgrib2 parm** (GFS · GEFS · AQM · HRRR) | NCEP's operational config that stamps the WMO header onto each model GRIB2 product (HRRR's are the `KWBY` parm files published per model version on the NCO server) |
| **MRMS SBN notices** | NWS bulletins assigning headers to MRMS products by name |
| **RTMA / URMA TINs** | NWS Technical Implementation Notices giving the header pieces for the analysis grids |
| **NOAAPort radar table** | NWS list of which NEXRAD Level III products are broadcast on the SBN and under which `SDUS` heading |
| **WMO / GRIB2 code tables + grib2io** | reference tables for what a field *is* (parameter, level, process), not header assignment |
| **XR-09 office directory** | official list of valid NWS office identifiers (CCCC), used to sanity-check the originating office |

Two layers are kept deliberately separate: **identity** (what a field is) and
**heading assignment** (which heading, if any, NWS gives it). grib2io answers the
first; only the parm files and notices answer the second. No GRIB library assigns
headings; that is a dissemination decision, not a property of the data.

## Project layout

| Path | Role |
| --- | --- |
| `read_icechunk.py` | CLI entry point; branches GRIB2-array vs CF-encoded GRIB2 vs radar |
| `grib_identity.py` / `radar_identity.py` | read a store's metadata into a product identity (`grib_identity` handles both the `grib_section3` and CF encodings) |
| `wmo_header.py` | match an identity to a header, or return `Unresolved` |
| `build_registry.py` | fetch, verify, and parse the pinned sources into the registry |
| `registry_db.py` | read/write the SQLite registry database |
| `tocgrib2_parm.py` · `nws_notice.py` · `nexrad_radar.py` | parsers for each source format |
| `registry/sources.json` | the pinned source manifest; the only hand-written data file |
| `requirements.txt` / `requirements-dev.txt` | pinned runtime and test dependencies |
| `tools/` | dev utilities: `inspect_icechunk.py`, `make_synthetic_l3.py` |
| `docs/` | diagrams (architecture, resolve example, registry schema) and notes |
| `tests/` | hermetic tests (no network) |

The registry itself (`registry/registry.db`, a SQLite file) is build output and
is not committed; `read_icechunk.py` builds it on first run. `sources.json` is
the audit trail: it pins every upstream document by tag and sha256, and the
build refuses to proceed on a mismatch. Every source is a public NWS / NCEP / WMO
document, cited per entry in the database.

### Registry database

The registry is a single SQLite file, `registry/registry.db`, holding the
product-to-heading mappings. It is built once (or on first run) and every lookup
thereafter is offline.

![registry schema](docs/registry_schema.png)

Three tables:

- **`source`**: one row per pinned document (MRMS notice, GFS config, radar
  table, …), recording where a set of mappings came from and any reference tables
  that document needs.
- **`entry`**: one row per product-to-heading assignment, belonging to a
  source. Holds the product description and its `TTAAII` / `CCCC` codes. A source
  has many entries.
- **`meta`**: build metadata (versions, checksums).

Each `entry` is stored as a JSON blob rather than fixed columns: the three
product types (notice, model, radar) populate different fields, so a new field
should not force a schema change.
