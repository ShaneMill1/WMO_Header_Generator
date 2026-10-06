# cirrus_wmo — WMO abbreviated heading resolver

Given a weather product stored in an icechunk / zarr archive, this finds the
**WMO abbreviated heading** (the "COMMS header") that the product is officially
disseminated under — or tells you, with a reason, that none exists.

It works for both GRIB2 model/analysis products (GFS, GEFS, AQM, MRMS, RTMA/URMA)
and NEXRAD radar (Level II and Level III).

Repository: https://github.com/ShaneMill1/WMO_Header_Generator

```
$ python read_icechunk.py
WMO: YAUP06 KWNR 301800
     product     : MultiSensor_QPE_24H_Pass2
     domain      : CONUS
     valid (UTC) : 2025-09-30T18:00
     authority   : nws-mrms-sbn -> MRMS-v12.2-supp
```

## How it works

![architecture](docs/architecture.png)

Four ideas, left to right:

1. **Input** — an icechunk / zarr store.
2. **Identify** — read the store's metadata to work out what the product is.
   GRIB2 and radar carry different metadata, so each has its own reader.
3. **Authoritative sources** — a registry built from pinned, hash-verified
   NWS / NCEP / WMO documents that record who assigned which header.
4. **Resolve** — match the product against those sources. Either it traces to a
   record (you get a header) or it does not (you get a clear reason).

## It is a resolver, not a generator

A heading cannot be computed from data. Its [form](https://www.weather.gov/tg/headef)
is fixed:

```
T1T2A1A2ii  CCCC  YYGGgg  (BBB)
```

but the specific `A2ii` and `CCCC` for a product are a **human allocation
decision**, made once when the product is added to dissemination and written
down in an operational config or a public notice. WMO's own definition of `ii`
is "a number to make this bulletin unique among ones with the same T1T2A1A2 and
CCCC" — it carries no derivable meaning.

Even the operational tool (`tocgrib2`) doesn't compute headings; it looks them up
in pre-written records and copies the string out, stamping only the `YYGGgg`
day/hour from the data. This project runs that lookup **backwards**: given
archived metadata, find the allocation someone already made. Where no allocation
exists, there is no heading to find.

## Accurate or nothing

A heading is emitted only when every field traces to an authoritative record.
Otherwise you get an `Unresolved` that says what was missing. The resolver never
guesses: it won't default a domain or originating office, won't invent a
timestamp, and won't pick between candidates the evidence can't distinguish. A
"no header" answer is often the *correct* answer — see the Demo for which
products have a heading and which don't.

## Demo

All output below is real. Commands use the project's Python environment.

### MRMS (GRIB2) — resolves

```
$ python read_icechunk.py --limit 5
WMO: YAUP06 KWNR 301800
     product     : MultiSensor_QPE_24H_Pass2
     description : MultiSensor_QPE_[01,03,06,12,24,48,72]H_Pass2
     domain      : CONUS
     valid (UTC) : 2025-09-30T18:00
     authority   : nws-mrms-sbn -> MRMS-v12.2-supp
     source group: /1/precipitation_accumulation_24_hour
Resolved 1 heading(s); 0 product(s) unresolved.
```

Add `--explain` to see the matching evidence and the verbatim source text.

### NEXRAD Level III, disseminated — resolves

A radar store is detected automatically; local stores use `--local`.

```
$ python read_icechunk.py --local /efs/cirrus_test_data/KTLX_165_0.5_icechunk
Store type: NEXRAD radar (CF/Radial)
WMO: SDUS8 KTLX 081617
     product     : Digital Hydrometeor Classification
     code         : 165
     elevation    : 0.5
     site (CCCC)  : KTLX
     authority    : nws-noaaport-radar -> noaaport-radar-products
Resolved 1 heading(s); 0 product(s) unresolved.
```

Matched by `(product code 165, elevation 0.5°)` → mnemonic `N0H` → `SDUS8`;
site `KTLX` becomes the CCCC; the time comes from the scan time.

### NEXRAD Level III, not disseminated — unresolved

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

### NEXRAD Level II — unresolved

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

### What resolves, and why

| Product | Resolves? | Why |
| --- | --- | --- |
| MRMS SBN product (e.g. 24-hour QPE) | ✅ `YAUP06 KWNR 301800` | assigned a header in the MRMS SBN notices |
| GFS / GEFS / AQM on an AWIPS grid | ✅ | assigned a header in the tocgrib2 parm config |
| RTMA / URMA analysis grid | ✅ | header pieces published in the NWS TINs |
| NEXRAD Level III, SBN product (e.g. 165 DHC) | ✅ `SDUS8 KTLX 081617` | listed in the NOAAPort radar table |
| NEXRAD Level III, not on the SBN (e.g. 167, 168) | ❌ | real product, but never broadcast under a heading |
| NEXRAD Level II base data | ❌ | distributed as whole-volume files (LDM/FTP), never headered |
| GFS at native 0.25° resolution | ❌ | regridded to an AWIPS grid before any header is applied |

A ❌ is the correct answer, not a gap: those products genuinely have no WMO
heading to find.

## Usage

```bash
# Just run it. The registry builds itself on first use.
python read_icechunk.py                          # default MRMS store (S3)
python read_icechunk.py --prefix path/to/store.ic --explain
python read_icechunk.py --local /path/to/radar_store   # local radar store

# Rebuild the registry explicitly (e.g. after editing sources.json).
python build_registry.py
python build_registry.py --source nws-noaaport-radar

python -m pytest tests/ -q                       # hermetic tests, no network
```

The default MRMS store is on S3 and needs AWS credentials in the environment;
local stores (e.g. radar on `/efs`) use `--local` and need none.

## The sources

The registry is built from pinned, hash-verified public documents. Each answers
a different piece of the question:

| Source | What it provides |
| --- | --- |
| **tocgrib2 parm** (GFS · GEFS · AQM) | NCEP's operational config that stamps the WMO header onto each model GRIB2 product |
| **MRMS SBN notices** | NWS bulletins assigning headers to MRMS products by name |
| **RTMA / URMA TINs** | NWS Technical Implementation Notices giving the header pieces for the analysis grids |
| **NOAAPort radar table** | NWS list of which NEXRAD Level III products are broadcast on the SBN and under which `SDUS` heading |
| **WMO / GRIB2 code tables + grib2io** | reference tables for what a field *is* (parameter, level, process) — not header assignment |
| **XR-09 office directory** | official list of valid NWS office identifiers (CCCC), used to sanity-check the originating office |

Two layers are kept deliberately separate: **identity** (what a field is) versus
**header assignment** (which header, if any, NWS gives it). grib2io answers the
first; only the parm files and notices answer the second. No GRIB library assigns
headers — that is a dissemination decision, not a property of the data.

## Project layout

| Path | Role |
| --- | --- |
| `read_icechunk.py` | CLI entry point; branches GRIB2 vs radar |
| `grib_identity.py` / `radar_identity.py` | read a store's metadata into a product identity |
| `wmo_header.py` | match an identity to a header, or return `Unresolved` |
| `build_registry.py` | fetch, verify, and parse the pinned sources into the registry |
| `tocgrib2_parm.py` · `nws_notice.py` · `nexrad_radar.py` | parsers for each source format |
| `registry/sources.json` | the pinned source manifest — the only hand-written data file |
| `tools/` | dev utilities: `inspect_icechunk.py`, `make_synthetic_l3.py` |
| `docs/` | architecture diagram and a step-by-step demo |
| `tests/` | hermetic tests (no network) |

The registry itself (`registry/*.jsonl.gz`, `*.manifest.json`) is build output
and is not committed; `read_icechunk.py` builds it on first run. `sources.json`
is the audit trail — it pins every upstream document by tag and sha256, and the
build refuses to proceed on a mismatch. Every source is a public NWS / NCEP / WMO
document, cited per entry in the generated manifests.
