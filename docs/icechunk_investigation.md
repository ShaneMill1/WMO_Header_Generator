## Inspect Existing CIRRUS Icechunk examples

Source: https://drive.google.com/drive/folders/15Hs4MD4qiJu6XUMGdbo3Eg9l5BbaWfKz

#### Abbreviations

- **L2 / L3**: NEXRAD radar data levels. L2 = Level II (base moments per sweep). L3 = Level III (derived single-product output).
- **NEXRAD**: Next-Generation Radar (the WSR-88D network).
- **VCP**: Volume Coverage Pattern — the scan strategy (set of elevation sweeps) the radar runs. `212` here.
- **Sweep**: one 360° rotation at a fixed elevation angle.
- **Moment / variable**:
  - **DBZ**: reflectivity (dBZ)
  - **VEL**: radial velocity (m/s)
  - **ZDR**: differential reflectivity (dB)
  - **PHI**: differential phase (degrees)
  - **RHO**: correlation coefficient (unitless)
  - **HC**: Raw CC — the single L3 moment in this example (product code 167)
- **Axes**:
  - **azimuth**: beam angle around the radar (degrees)
  - **range**: distance along the beam (meters)
  - **ray**: L3 beam index (one per azimuth)
  - **gate**: L3 range-bin index (one per range step)
  - **pdv**: product-dependent values — L3 metadata array
- **CF/Radial**: the Climate and Forecast metadata convention for radar data (`Conventions` attribute).

#### Radar L2 Icechunk

Notes:

- Multiple-group icechunk. Each sweep is its own group.
- 5 groups total: 1 root (`/`) + 4 sweeps (`sweep_0`–`sweep_3`). The root holds site metadata (lat/lon/alt variables plus site/instrument global attributes).
- Coordinates are 1D per sweep: `azimuth(azimuth)`, `range(range)`, `time(time)`.
- Moment set varies by sweep: sweep_0 has DBZ/ZDR/PHI/RHO (no VEL); sweeps 1–3 add VEL. Gate counts vary (1832, 1192, 1832, 1712); sweep_3 has fewer azimuths (479).

```
(cirrus_wmo) [shane.mill@ip-205-156-8-80 cirrus_wmo]$ /home/shane.mill/mambaforge/envs/cirrus_wmo/bin/python inspect_icechunk.py /efs/cirrus_test_data/KDFX_icechunk

Opening /efs/cirrus_test_data/KDFX_icechunk (branch=main)

  2026-10-06T15:09:32.101614Z  WARN icechunk_arrow_object_store: The LocalFileSystem storage is not safe for concurrent commits. If more than one thread/process will attempt to commit at the same time, prefer using object stores.

    at icechunk-arrow-object-store/src/lib.rs:330

group: /

  var    : altitude () () float64

  var    : latitude () () float64

  var    : longitude () () float64

  gattr  : site_id = KDFX

  gattr  : site_name = KDFX

  gattr  : instrument_name = NEXRAD

  gattr  : scan_time = 2026-07-16T18:53:06.150000+00:00

  gattr  : vcp_number = 212

  gattr  : Conventions = CF/Radial

group: /sweep_0

  dims   : azimuth=720, range=1832, time=1

  coord  : azimuth ('azimuth',) float64

  coord  : time ('time',) datetime64[ns]

  coord  : range ('range',) float64

  var    : ZDR ('azimuth', 'range') (720, 1832) float32

             attr long_name = Differential Reflectivity

             attr units = dB

             attr valid_min = -5.0

             attr valid_max = 10.0

  var    : PHI ('azimuth', 'range') (720, 1832) float32

             attr long_name = Differential Phase

             attr units = degrees

             attr valid_min = 0.0

             attr valid_max = 360.0

  var    : RHO ('azimuth', 'range') (720, 1832) float32

             attr long_name = Correlation Coefficient

             attr units = 

             attr valid_min = 0.0

             attr valid_max = 1.0

  var    : DBZ ('azimuth', 'range') (720, 1832) float32

             attr long_name = Reflectivity

             attr units = dBZ

             attr valid_min = -20.0

             attr valid_max = 80.0

  gattr  : sweep_number = 0

  gattr  : sweep_mode = azimuth_surveillance

  gattr  : fixed_angle = 0.48

group: /sweep_1

  dims   : azimuth=720, range=1192, time=1

  coord  : range ('range',) float64

  coord  : time ('time',) datetime64[ns]

  coord  : azimuth ('azimuth',) float64

  var    : DBZ ('azimuth', 'range') (720, 1192) float32

             attr long_name = Reflectivity

             attr units = dBZ

             attr valid_min = -20.0

             attr valid_max = 80.0

  var    : RHO ('azimuth', 'range') (720, 1192) float32

             attr long_name = Correlation Coefficient

             attr units = 

             attr valid_min = 0.0

             attr valid_max = 1.0

  var    : ZDR ('azimuth', 'range') (720, 1192) float32

             attr long_name = Differential Reflectivity

             attr units = dB

             attr valid_min = -5.0

             attr valid_max = 10.0

  var    : PHI ('azimuth', 'range') (720, 1192) float32

             attr long_name = Differential Phase

             attr units = degrees

             attr valid_min = 0.0

             attr valid_max = 360.0

  var    : VEL ('azimuth', 'range') (720, 1192) float32

             attr long_name = Velocity

             attr units = m/s

             attr valid_min = -50.0

             attr valid_max = 50.0

  gattr  : sweep_number = 1

  gattr  : sweep_mode = azimuth_surveillance

  gattr  : fixed_angle = 0.53

group: /sweep_2

  dims   : azimuth=720, range=1832, time=1

  coord  : azimuth ('azimuth',) float64

  coord  : time ('time',) datetime64[ns]

  coord  : range ('range',) float64

  var    : DBZ ('azimuth', 'range') (720, 1832) float32

             attr long_name = Reflectivity

             attr units = dBZ

             attr valid_min = -20.0

             attr valid_max = 80.0

  var    : PHI ('azimuth', 'range') (720, 1832) float32

             attr long_name = Differential Phase

             attr units = degrees

             attr valid_min = 0.0

             attr valid_max = 360.0

  var    : VEL ('azimuth', 'range') (720, 1832) float32

             attr long_name = Velocity

             attr units = m/s

             attr valid_min = -50.0

             attr valid_max = 50.0

  var    : ZDR ('azimuth', 'range') (720, 1832) float32

             attr long_name = Differential Reflectivity

             attr units = dB

             attr valid_min = -5.0

             attr valid_max = 10.0

  var    : RHO ('azimuth', 'range') (720, 1832) float32

             attr long_name = Correlation Coefficient

             attr units = 

             attr valid_min = 0.0

             attr valid_max = 1.0

  gattr  : sweep_number = 2

  gattr  : sweep_mode = azimuth_surveillance

  gattr  : fixed_angle = 0.92

group: /sweep_3

  dims   : azimuth=479, range=1712, time=1

  coord  : range ('range',) float64

  coord  : time ('time',) datetime64[ns]

  coord  : azimuth ('azimuth',) float64

  var    : DBZ ('azimuth', 'range') (479, 1712) float32

             attr long_name = Reflectivity

             attr units = dBZ

             attr valid_min = -20.0

             attr valid_max = 80.0

  var    : PHI ('azimuth', 'range') (479, 1712) float32

             attr long_name = Differential Phase

             attr units = degrees

             attr valid_min = 0.0

             attr valid_max = 360.0

  var    : ZDR ('azimuth', 'range') (479, 1712) float32

             attr long_name = Differential Reflectivity

             attr units = dB

             attr valid_min = -5.0

             attr valid_max = 10.0

  var    : RHO ('azimuth', 'range') (479, 1712) float32

             attr long_name = Correlation Coefficient

             attr units = 

             attr valid_min = 0.0

             attr valid_max = 1.0

  gattr  : sweep_number = 3

  gattr  : sweep_mode = azimuth_surveillance

  gattr  : fixed_angle = 1.27
```

##### Investigate Individual Groups Opened as Xarray Datasets:

###### Sweep 0:

```
(cirrus_wmo) [shane.mill@ip-205-156-8-80 cirrus_wmo]$ /home/shane.mill/mambaforge/envs/cirrus_wmo/bin/python inspect_icechunk.py /efs/cirrus_test_data/KDFX_icechunk --group /sweep_0

Opening /efs/cirrus_test_data/KDFX_icechunk (branch=main)

===== group: /sweep_0 =====

<xarray.Dataset> Size: 21MB

Dimensions:  (azimuth: 720, range: 1832, time: 1)

Coordinates:

  * azimuth  (azimuth) float64 6kB 74.49 74.98 75.48 75.97 ... 72.94 73.44 73.96

  * range    (range) float64 15kB 2.125e+03 2.375e+03 ... 4.596e+05 4.599e+05

  * time     (time) datetime64[ns] 8B 2026-07-16T18:53:06.150000

Data variables:

    DBZ      (azimuth, range) float32 5MB ...

    RHO      (azimuth, range) float32 5MB ...

    ZDR      (azimuth, range) float32 5MB ...

    PHI      (azimuth, range) float32 5MB ...

Attributes:

    sweep_number:  0

    sweep_mode:    azimuth_surveillance

    fixed_angle:   0.48
```

###### Sweep 1:

```
(cirrus_wmo) [shane.mill@ip-205-156-8-80 cirrus_wmo]$ /home/shane.mill/mambaforge/envs/cirrus_wmo/bin/python inspect_icechunk.py /efs/cirrus_test_data/KDFX_icechunk --group /sweep_1

Opening /efs/cirrus_test_data/KDFX_icechunk (branch=main)

===== group: /sweep_1 =====

<xarray.Dataset> Size: 17MB

Dimensions:  (azimuth: 720, range: 1192, time: 1)

Coordinates:

  * azimuth  (azimuth) float64 6kB 101.4 101.9 102.4 102.9 ... 99.96 100.5 100.9

  * range    (range) float64 10kB 2.125e+03 2.375e+03 ... 2.996e+05 2.999e+05

  * time     (time) datetime64[ns] 8B 2026-07-16T18:53:06.150000

Data variables:

    DBZ      (azimuth, range) float32 3MB ...

    PHI      (azimuth, range) float32 3MB ...

    VEL      (azimuth, range) float32 3MB ...

    ZDR      (azimuth, range) float32 3MB ...

    RHO      (azimuth, range) float32 3MB ...

Attributes:

    sweep_number:  1

    sweep_mode:    azimuth_surveillance

    fixed_angle:   0.53
```

###### Sweep 2:

```
(cirrus_wmo) [shane.mill@ip-205-156-8-80 cirrus_wmo]$ /home/shane.mill/mambaforge/envs/cirrus_wmo/bin/python inspect_icechunk.py /efs/cirrus_test_data/KDFX_icechunk --group /sweep_2

Opening /efs/cirrus_test_data/KDFX_icechunk (branch=main)

===== group: /sweep_2 =====

<xarray.Dataset> Size: 26MB

Dimensions:  (azimuth: 720, range: 1832, time: 1)

Coordinates:

  * azimuth  (azimuth) float64 6kB 115.5 116.0 116.5 117.0 ... 114.0 114.5 115.0

  * range    (range) float64 15kB 2.125e+03 2.375e+03 ... 4.596e+05 4.599e+05

  * time     (time) datetime64[ns] 8B 2026-07-16T18:53:06.150000

Data variables:

    PHI      (azimuth, range) float32 5MB ...

    DBZ      (azimuth, range) float32 5MB ...

    VEL      (azimuth, range) float32 5MB ...

    ZDR      (azimuth, range) float32 5MB ...

    RHO      (azimuth, range) float32 5MB ...

Attributes:

    sweep_number:  2

    sweep_mode:    azimuth_surveillance

    fixed_angle:   0.92
```

###### Sweep 3:

```
(cirrus_wmo) [shane.mill@ip-205-156-8-80 cirrus_wmo]$ /home/shane.mill/mambaforge/envs/cirrus_wmo/bin/python inspect_icechunk.py /efs/cirrus_test_data/KDFX_icechunk --group /sweep_3

Opening /efs/cirrus_test_data/KDFX_icechunk (branch=main)

===== group: /sweep_3 =====

<xarray.Dataset> Size: 13MB

Dimensions:  (azimuth: 479, range: 1712, time: 1)

Coordinates:

  * azimuth  (azimuth) float64 4kB 204.5 205.0 205.5 206.0 ... 82.45 82.95 143.7

  * range    (range) float64 14kB 2.125e+03 2.375e+03 ... 4.296e+05 4.299e+05

  * time     (time) datetime64[ns] 8B 2026-07-16T18:53:06.150000

Data variables:

    DBZ      (azimuth, range) float32 3MB ...

    PHI      (azimuth, range) float32 3MB ...

    ZDR      (azimuth, range) float32 3MB ...

    RHO      (azimuth, range) float32 3MB ...

Attributes:

    sweep_number:  3

    sweep_mode:    azimuth_surveillance

    fixed_angle:   1.27
```

#### Radar L3 Icechunk

Notes:

- Multiple-group icechunk, but deeper than L2: data lives at `/<site>/<productcode>_<moment>` (two levels under root). L2 sweeps sit one level under root at `/sweep_N`.
- Single data group: `/KTLX/167_HC` — one L3 product (product code 167, "Raw CC", moment `HC`) for site KTLX.
- One product/moment at one elevation (`elevation_angle`), matching the `0.5` in the store name. No per-sweep fan-out.
- Coordinates are 2D and time-prefixed: `azimuth(time, ray)`, `range(time, gate)`. L2 is 1D: `azimuth(azimuth)`, `range(range)`.
- L3-only coords: `product_dependent_values(time, pdv)` and `quality_control_flags_json(time)`.
- Site metadata (lat/lon/alt, site_id, vcp) lives on the product group's attributes. No root data group.

```
(cirrus_wmo) [shane.mill@ip-205-156-8-80 cirrus_wmo]$ /home/shane.mill/mambaforge/envs/cirrus_wmo/bin/python inspect_icechunk.py /efs/cirrus_test_data/KTLX_167_0.5_icechunk

Opening /efs/cirrus_test_data/KTLX_167_0.5_icechunk (branch=main)

  2026-10-06T15:14:16.455899Z  WARN icechunk_arrow_object_store: The LocalFileSystem storage is not safe for concurrent commits. If more than one thread/process will attempt to commit at the same time, prefer using object stores.

    at icechunk-arrow-object-store/src/lib.rs:330

group: /KTLX/167_HC

  dims   : time=1, ray=720, gate=1200, pdv=10

  coord  : azimuth ('time', 'ray') float32

  coord  : product_dependent_values ('time', 'pdv') int64

  coord  : range ('time', 'gate') float32

  coord  : elevation_angle ('time',) float64

  coord  : time ('time',) datetime64[ns]

  coord  : quality_control_flags_json ('time',) object

  var    : HC ('time', 'ray', 'gate') (1, 720, 1200) float32

             attr long_name = Raw CC

             attr units = raw

             attr valid_min = 0.0

             attr valid_max = 10.0

  gattr  : product_code = 167

  gattr  : product_name = Raw CC

  gattr  : moment_name = HC

  gattr  : site_id = KTLX

  gattr  : latitude = 35.333

  gattr  : longitude = -97.278

  gattr  : altitude = 1277.0

  gattr  : vcp_number = 212

  gattr  : scan_time = 2026-01-08T16:17:14+00:00

  gattr  : Conventions = CF/Radial
```

##### Investigate Individual Groups Opened as Xarray Datasets:

###### KTLX / 167_HC:

```
(cirrus_wmo) [shane.mill@ip-205-156-8-80 cirrus_wmo]$ /home/shane.mill/mambaforge/envs/cirrus_wmo/bin/python inspect_icechunk.py /efs/cirrus_test_data/KTLX_167_0.5_icechunk --group /KTLX/167_HC

Opening /efs/cirrus_test_data/KTLX_167_0.5_icechunk (branch=main)

===== group: /KTLX/167_HC =====

<xarray.Dataset> Size: 3MB

Dimensions:                     (time: 1, ray: 720, gate: 1200, pdv: 10)

Coordinates:

  * time                        (time) datetime64[ns] 8B 2026-01-08T16:17:14

    quality_control_flags_json  (time) object 8B ...

    elevation_angle             (time) float64 8B ...

    azimuth                     (time, ray) float32 3kB ...

    range                       (time, gate) float32 5kB ...

    product_dependent_values    (time, pdv) int64 80B ...

Dimensions without coordinates: ray, gate, pdv

Data variables:

    HC                          (time, ray, gate) float32 3MB ...

Attributes:

    product_code:  167

    product_name:  Raw CC

    moment_name:   HC

    site_id:       KTLX

    latitude:      35.333

    longitude:     -97.278

    altitude:      1277.0

    vcp_number:    212

    scan_time:     2026-01-08T16:17:14+00:00

    Conventions:   CF/Radial
```

#### HRRR Icechunk

_(TODO: HRRR store is still a `.zip.filepart` upload in `/efs/cirrus_test_data` — inspect once it finishes transferring.)_

---

## Key Takeaways: Schema Differences

Both stores use `Conventions = CF/Radial` and the same polar geometry (azimuth × range, VCP 212). The layout in the icechunk tree differs as follows.

**1. Group layout.** Both have a root `/` group; the difference is depth and fan-out.
- **L2**: a single level of groups under root — one group per sweep (`/sweep_0` … `/sweep_3`, 4 sweeps here). Multiple sibling data groups, each a separate elevation cut.
- **L3**: a deeper, multi-level path — `/<site>/<productcode>_<moment>` (e.g. `/KTLX/167_HC`), with the data group two levels down. A single data group for one product at one elevation.

**2. Where site metadata lives.**
- **L2** puts site location in the **root group** as actual variables (`latitude`, `longitude`, `altitude`) plus global attrs (`site_id`, `site_name`, `instrument_name`, `vcp_number`).
- **L3** puts the same information on the **product group's attributes** (`site_id`, `latitude`, `longitude`, `altitude`, `vcp_number` as scalars). There is no central metadata group to read first.

**3. Coordinate dimensionality.**
- **L2** coordinates are **1D**: `azimuth(azimuth)`, `range(range)`, `time(time)`.
- **L3** coordinates are **2D / time-prefixed**: `azimuth(time, ray)`, `range(time, gate)`, with the beam/gate axes named `ray`/`gate` instead of `azimuth`/`range`, and those axes have no coordinate variables ("dimensions without coordinates").

**4. Dimension names.**
- **L2** uses `azimuth` and `range` as both dimension and coordinate names.
- **L3** separates them: dimensions are `ray` and `gate`, while `azimuth` and `range` are 2D coordinates mapped onto them. L3 also adds a `pdv` (product-dependent values) dimension.

**5. Variable / moment content.**
- **L2** carries the full moment set per sweep (DBZ, ZDR, PHI, RHO, and VEL on sweeps 1–3), and gate/azimuth counts vary sweep to sweep (e.g. sweep_3 is 479 × 1712).
- **L3** carries a **single moment** per store (`HC` here, "Raw CC"), identified by `product_code` / `moment_name`. A different L3 product would be a different store/group.

**6. L3-only coordinates.**
- `product_dependent_values(time, pdv)` and `quality_control_flags_json(time)` exist only in L3 and have no L2 analogue.

**Downstream implication.** A reader handling both must:
- discover groups instead of assuming `/sweep_N`
- read site metadata from two places: root variables (L2) vs group attrs (L3)
- handle both 1D (`azimuth`/`range`) and 2D (`ray`/`gate` with 2D `azimuth`/`range`) coordinates

Normalize these at read time; one code path for both will break otherwise.

---

## Authoritative Source for Radar WMO Headers

Motivation: map a radar icechunk store to a WMO abbreviated heading, the same way `cirrus_wmo` already does for GRIB2/MRMS. That requires an authoritative source that assigns headers to radar products. Found one.

### The source

**NOAAPort Radar Products table** — `https://www.weather.gov/media/tg/noaaport_radar_products.pdf` (5 pages, "updated May 09, 2025"). Lists every WSR-88D and TDWR product disseminated on the SBN, keyed by product code. This is the radar analog of the MRMS v12.2 supplemental: a single public NWS table that assigns headers by product, so it fits the existing `nws_notice` source kind.

Staged copies (with the SCN change-history notices) are in `/efs/cirrus_test_data/radar_headers_docs/`:

| File | sha256 (first 16) | Role |
| --- | --- | --- |
| `noaaport_radar_products.pdf` | `0d9ef6019b8a47b7` | master table (primary authority) |
| `scn24-09_nexrad_l3_dissemination.pdf` | `eb316476ed59b250` | L3 dissemination change (Build 22.1) |
| `scn24-73_basetilt_central_collection.pdf` | `9497ee4023a1d530` | BaseTilt central collection (Build 23) |
| `scn26-20_basetilt_kbox.pdf` | `02deadc7b2264cea` | BaseTilt KBOX (Build 24.1) |
| `roc_icd_2620003AE_build24_product_spec.pdf` | `9a90b8c728d6bd7c` | WSR-88D ROC ICD — Level III product-code table (authoritative code→product→mnemonic) |

### Synthetic fixture for the resolve path

Both real samples (KDFX L2, KTLX 167) correctly resolve to nothing, so neither exercises the metadata→heading path. To develop/test that path, a synthetic *disseminated* L3 store is generated by `cirrus_wmo/make_synthetic_l3.py`:

- Output: `/efs/cirrus_test_data/KTLX_165_0.5_icechunk`, group `/KTLX/165_DHC`.
- Schema is byte-for-byte the same shape as the real KTLX 167 store (dims, coords, object-dtype QC JSON, "dimensions without coordinates").
- Differs only in resolution-relevant fields: `product_code=165` (Digital Hydrometeor Classification — `SDUS8i`/`N0H` on the SBN), `moment_name="DHC"`, `elevation_angle=0.5` (→ NNN mnemonic `N0H`).
- Array values are synthetic/throwaway; only structure + identifying metadata matter. The store is regenerable from the script, so it need not be committed.

Expected once the radar resolver exists: this store resolves to an `SDUS8i KTLX … N0H` heading, while KDFX L2 and KTLX 167 resolve to Unresolved. The three together cover both resolver branches.

Parses cleanly with `pypdf` (already a build_registry dependency) — no OCR needed.

### Table structure

Each row:

```
{product_code}/{RPG_HEADER}   SDUS{tier}i cccc   {NNN} xxx   {elevation}
```

Example rows (rephrased for licensing compliance):
- `161/DCC  SDUS8i cccc  NXC xxx  -0.2`  → Digital Correlation Coefficient, tilt -0.2°
- `161/DCC  SDUS8i cccc  N0C xxx  0.5`   → same product, 0.5° tilt
- `165/DHC  SDUS8i cccc  N0H xxx  0.5`   → Digital Hydrometeor Classification, 0.5°
- `177/HHC  SDUS8i cccc  HHC xxx`        → Hybrid Scan Hydrometeor Classification (no elevation)

### Heading composition

```
TTAAII = SDUS{tier}i      (SD = radar imagery; tier digit = SBN channel/priority; trailing i is a placeholder)
NNN    = AWIPS mnemonic   (e.g. NXC, N0C, HHC) — encodes product + elevation tilt
cccc   = originating site (from the store's site_id)
YYGGgg = from scan_time   (format_yygggg already handles this)
```

### How it joins the icechunk metadata

| Store attr (L3) | Table column |
| --- | --- |
| `product_code` (e.g. 161) | the `{code}` in `{code}/{RPG_HEADER}` |
| `elevation_angle` | the elevation column — selects which NNN within a product |
| `site_id` (e.g. KTLX) | the `cccc`/`xxx` site fill |
| `scan_time` | YYGGgg |

Key insight: the heading is keyed on **product code + elevation angle**, not product code alone — one product code maps to up to 9 NNN mnemonics, one per tilt. The L3 store's `elevation_angle` coord is the radar analog of GRIB2's level axis.

### Open questions before building

1. **Product code 167 — RESOLVED: it is a real code, but not SBN-disseminated.** Reconciled against the WSR-88D ROC ICD 2620003 Level III product-code table (and the Supercell-Wx mirror of it). Code 167 = **"Super Res Digital Correlation Coefficient"**, the super-resolution variant of code 161 (Digital Correlation Coefficient). The KTLX store's `moment_name=HC` / `product_name="Raw CC"` (Raw Correlation Coefficient) matches 167 — it is **not** a mislabel.

   The decisive detail: codes 159/161/163/165/166 each have a full set of NNN mnemonics (`NXC…N3C`, etc.) and appear in the NOAAPort/SBN table with `SDUS8i` headings. Codes **167 and 168** ("Super Res Digital Phi") have a **blank mnemonic column in the ICD and no row in the NOAAPort/SBN table** — they are defined products but are **not disseminated on the SBN under a WMO heading**. So, like Level II, product code 167 has no authoritative WMO heading, and the correct result under Option A is `Unresolved`.

   **Generalized finding:** L3 splits into two classes — *disseminated* L3 (159, 161, 163, 165, 166, 177, …; have SDUS headings → resolve) and *defined-but-not-disseminated* L3 (167, 168, super-res internal products; no heading → Unresolved). The resolver must distinguish them by presence in the NOAAPort/SBN table, not merely by being "a valid L3 product code".
2. **Tier-digit drift.** The SCNs reassign products across SDUS tiers (5↔6↔8) per build. Treat the master table as the current authority and the SCNs as change history; if sources disagree on a code, mark it unusable (same handling as the MRMS `YAUS06` contradiction).
3. **Level II has no WMO heading — DECIDED: resolve to Unresolved (Option A).** L2 base data (the KDFX store) is distributed as whole-volume files, not as WMO-headered bulletins. Verified from three independent authorities:
   - NCEI td6500: L2 is distributed in near real-time from NWS Regional servers (file distribution).
   - NWS Central Radar Server (weather.gov/tg/radfiles): obtained via LDM feed or FTP.
   - Unidata LDM docs: LDM routes by *feedtype + product identifier* (regex-matched), not by WMO header. For Level II (CRAFT/NEXRAD2) the identifier is a volume/chunk name (last chunk flagged with `/E`), and the Archive II format contains no WMO abbreviated heading. To insert a file into LDM *with* a WMO identifier you must bake one into the filename and use pqinsert — proof LDM does not supply one.

   "Came over LDM" therefore does **not** imply "has a WMO header." Every WMO-heading document (the SDUS/NOAAPort tables) is Level III only. So under "accurate or nothing", L2 resolves to `Unresolved` with a cited reason: *"Level II base data are distributed as whole-volume files via LDM/FTP, not under a WMO abbreviated heading; no authoritative record assigns one."* This mirrors the existing treatment of the regridded 0.25° GFS (correctly resolves to nothing). **L3 is the only radar target that produces a header.**

### Next build steps

1. Add a source entry to `registry/sources.json` (kind `nws_notice`, or a new `nexrad_radar` kind) pinning `noaaport_radar_products.pdf` by sha256.
2. Add a `build_nexrad_radar_source(...)` in `build_registry.py` that parses the table into entries: `{product_code, nnn, ttaaii_tier, elevation, product_description, source_...}`.
3. Resolve the product-code crosswalk (open question 1) so `product_code=167` maps to a table row.
4. Add `radar_identity.py` (`extract_radar_identity`) reading CF/Radial attrs: `product_code`, `moment_name`, `elevation_angle`, `site_id`, `scan_time`.
5. Add `resolve_radar(...)` in `wmo_header.py`, keyed on (product_code, elevation) → NNN → heading; wire into `resolve()`.
6. Branch `read_icechunk.py` on `Conventions == CF/Radial` to use the radar path; fan out over sweeps/products instead of levels×times.
7. L2 (Option A): the radar extractor/resolver detects a Level II store (no `product_code`; site-level root metadata; `instrument_name=NEXRAD`) and returns `Unresolved` with the cited reason above. No L2 header source is added, because none exists. Add a test asserting L2 moments resolve to Unresolved with that reason.
