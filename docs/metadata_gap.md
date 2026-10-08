# Metadata gap analysis: cirrus (efs) vs nwsviz (S3)

This records a metadata comparison between the two store families the resolver
reads: the CIRRUS stores on `/efs/cirrus_test_data` and the nwsviz stores in S3
(`wsupviewer-eks-transfer`). It is a comparison of the metadata each side
exposes, not of header-resolution results. The question is what a store tells you
about its data, which is what decides what the resolver can key on.

Produced with `tools/metadata_inventory.py`. See "Reproducing" below.

## Samples compared

- **cirrus** (3 stores): one CF-encoded HRRR store and two NEXRAD radar stores
  (Level III `KTLX_165`, Level II `KDFX`).
- **nwsviz** (4 stores): one representative store each for MRMS, GFS 0.25°,
  RTMA 2.5°, and GEFS 0.25°.

The nwsviz sample is one store per model, not every store, so coverage-type
findings (for example ensemble fields) reflect the sample, not the whole bucket.

## Headline finding

Both families encode the same underlying information, but in different places and
with different conventions. The single largest difference is **where a product's
identity lives**:

- **nwsviz**: the variable describes itself. Each data variable carries the full
  GRIB2 identity as its own attributes (`short_name`, `grib_section3`,
  `product_definition_template_number`, the fixed-surface and generating-process
  types, statistical process, originating center, and so on). The resolver reads
  identity straight off the variable.
- **cirrus**: the variable is sparse (`long_name`, `units`, `valid_min`,
  `valid_max`). Identity lives one level up: HRRR puts `code`/`name` on the
  group; radar puts `site_id`/`product_code`/`scan_time` on the root or a nested
  group.

This is the empirical justification for having three identity readers rather than
one: a self-describing GRIB2-array variable, a CF-encoded store whose identity is
on the group, and a CF/Radial radar store whose identity is on the root or a
nested group are genuinely different metadata shapes.

## Dimension-by-dimension

### Grid encoding

| | convention |
| --- | --- |
| nwsviz | `grib_section3` attribute on the variable, plus `latlon_coords`; Lambert stores (MRMS, RTMA) also carry `projected_xy` |
| cirrus | CF `spatial_ref` grid-mapping with `projected_xy` |

Both carry `projected_xy`, so the split is not simply projected vs lat/lon. The
real divergence is the container: nwsviz keeps the raw GRIB2 Section 3 array as a
variable attribute, cirrus keeps a CF grid-mapping. Same grid concept, different
encoding. `extract_identity` reads the first, `extract_identity_cf` reconstructs
Section 3 from the second.

### Where identity lives

| | location |
| --- | --- |
| nwsviz | `variable_attrs` (`short_name` on the data variable) |
| cirrus | `group_attrs` (HRRR `code`/`name`) and `root_attrs` (radar `site_id`) |

No overlap. This is the difference that drives the separate readers.

### Level encoding

| | convention |
| --- | --- |
| nwsviz | vertical coordinate named by physical surface type: `isobaric_surface`, `specified_height_level_above_ground`, `mean_sea_level`, `level_at_specified_pressure_difference_from_ground_to_level`, ... |
| cirrus | raw GRIB2 fixed-surface primitives as coords: `scaled_value_of_first_fixed_surface`, `type_of_second_fixed_surface`, `level_start`/`level_end`, `level` |

Same information, opposite philosophy. nwsviz pre-interprets the fixed surface
into a named coordinate; cirrus stores the GRIB2 machinery and leaves
interpretation to the reader.

### Time encoding

Shared: `time`, an init/reference time (`init_time` on cirrus,
`forecast_reference_time` on nwsviz), and `lead_time`. No gap. The resolver's
timestamp handling works the same way on both.

### Ensemble

nwsviz carries `perturbation_number` (the GEFS member dimension); the cirrus
sample has no equivalent because it contains no ensemble product. This is a
coverage difference in the sample, not a convention difference.

## What it means for the resolver

- The architecture is confirmed, not challenged. The three identity readers map
  directly onto the three metadata shapes this diff found.
- For nwsviz the GRIB2-array path has everything inline (`short_name`,
  `grib_section3`, the PDT fields). For cirrus the CF path reconstructs the same
  identity from `spatial_ref` + `x`/`y` + the scaled-value level coords, and the
  radar path reads site and product from the root or nested group.
- No change is needed to time handling. The level-encoding and grid-encoding
  differences are already absorbed by the per-encoding readers.

In short, the gap is one of encoding convention, not of missing information: the
same identity exists on both sides, and each reader knows where to find it.

## Reproducing

S3 access needs AWS credentials in the environment; local stores need none.

```bash
# nwsviz side (S3)
python tools/metadata_inventory.py inventory --name nwsviz \
  --prefix edr-api/mrms_v12p2/2021-01/mrms_v12p2-conus_lambert-2021-01.ic \
  --prefix edr-api/gfs0p25_v16p3/2021-01-01T00:00/gfs0p25_v16p3-global_latlon-2021-01-01T00:00.ic \
  --prefix edr-api/rtma2p5_v2p10/2017-05/rtma2p5_v2p10-conus_lambert-2017-05.ic \
  --prefix edr-api/gefs0p25_v12p3/2021-01-01T00:00/gefs0p25_v12p3-global_latlon-2021-01-01T00:00.ic \
  --out nwsviz_meta.json

# cirrus side (local)
python tools/metadata_inventory.py inventory --name cirrus \
  --local /efs/cirrus_test_data/HRRR-2026-09-21-sample-v2.icechunk \
  --local /efs/cirrus_test_data/KTLX_165_0.5_icechunk \
  --local /efs/cirrus_test_data/KDFX_icechunk \
  --out cirrus_meta.json

# compare
python tools/metadata_inventory.py diff cirrus_meta.json nwsviz_meta.json
```

The `*_meta.json` dumps are build output and are not committed; regenerate them
with the commands above. Expand the `--prefix` list to widen the nwsviz sample.
