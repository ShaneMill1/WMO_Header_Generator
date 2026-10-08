# Metadata gap analysis: cirrus (efs) vs nwsviz (S3)

A comparison of the metadata each store family exposes: the CIRRUS stores on
`/efs/cirrus_test_data` and the nwsviz stores in S3 (`wsupviewer-eks-transfer`).
This looks only at what a store tells you about its data, which attributes,
coordinates, and encoding conventions it carries.

## Samples compared

- **cirrus** (3 stores): one CF-encoded HRRR store and two NEXRAD radar stores
  (Level III `KTLX_165`, Level II `KDFX`).
- **nwsviz** (4 stores): one representative store each for MRMS, GFS 0.25°,
  RTMA 2.5°, and GEFS 0.25°.

The nwsviz sample is one store per model, not every store, so coverage-type
observations (for example ensemble fields) reflect the sample, not the whole
bucket.

## Side by side

| Dimension | nwsviz (S3) | cirrus (efs) |
| --- | --- | --- |
| **Where identity lives** | on the data variable (`short_name` and full GRIB2 identity as variable attributes) | on the group (HRRR `code`/`name`) or on the root / nested group (radar `site_id`, `product_code`, `scan_time`); the variable itself is sparse |
| **Grid encoding** | `grib_section3` array as a variable attribute; `latlon_coords`, with `projected_xy` on Lambert stores (MRMS, RTMA) | CF `spatial_ref` grid-mapping with `projected_xy`; radar as CF/Radial |
| **Level encoding** | vertical coordinate named by physical surface type (`isobaric_surface`, `specified_height_level_above_ground`, `mean_sea_level`, `level_at_specified_pressure_difference_from_ground_to_level`, ...) | raw GRIB2 fixed-surface primitives as coords (`scaled_value_of_first_fixed_surface`, `type_of_second_fixed_surface`, `level_start`/`level_end`, `level`) |
| **Time encoding** | `time`, `forecast_reference_time`, `lead_time` | `time`, `init_time`, `lead_time` |
| **Variable attributes** | rich: `short_name`, `standard_name`, `grib_section3`, `product_definition_template_number`, `type_of_first/second_fixed_surface`, `type_of_generating_process`, `statistical_process`, `cell_methods`, `originating_center`/`sub_center`, `master_table_info`, grid geometry (`gridlength_*`, `latitude_first_gridpoint`, ...) | sparse: `long_name`, `units`, `valid_min`, `valid_max` |
| **Group attributes** | grid geometry (`crs_wkt`, `gridlength_x_direction`, `gridlength_y_direction`, `latitude_first_gridpoint`, `longitude_first_gridpoint`) | **model (HRRR):** `code`, `name`, `units`<br>**radar:** `site_id`, `product_code`, `product_name`, `moment_name`, `scan_time`, `vcp_number`, `fixed_angle`, `sweep_mode`, `sweep_number`, `latitude`, `longitude`, `altitude` |
| **Root attributes** | none of note | **model (HRRR):** `processed_files` (provenance only)<br>**radar:** `Conventions`, `instrument_name`, `site_id`, `site_name`, `scan_time`, `vcp_number` |
| **Ensemble** | `perturbation_number` (GEFS member dimension) | none in the sample |

## Headline difference

Both families encode the same underlying information, but in different places and
with different conventions. The largest difference is **where a product's
identity lives**:

- **nwsviz**: the variable describes itself. Each data variable carries the full
  GRIB2 identity as its own attributes.
- **cirrus**: the variable is sparse. Identity lives one level up, on the group
  (HRRR) or on the root / a nested group (radar).

## Notes on each dimension

### Grid encoding

Both carry `projected_xy`, so the split is not simply projected vs lat/lon. The
real divergence is the container: nwsviz keeps the raw GRIB2 Section 3 array as a
variable attribute, cirrus keeps a CF `spatial_ref` grid-mapping. Same grid
concept, different encoding.

### Level encoding

Same information, opposite philosophy. nwsviz pre-interprets the fixed surface
into a named coordinate; cirrus stores the GRIB2 fixed-surface primitives and
leaves the interpretation to whatever consumes the store.

### Time encoding

Effectively shared: both carry a valid time, an initialization / reference time
(`init_time` on cirrus, `forecast_reference_time` on nwsviz), and a lead time.
No gap.

### Ensemble

nwsviz carries `perturbation_number`; the cirrus sample has no equivalent because
it contains no ensemble product. This is a coverage difference in the sample, not
a convention difference.

## Summary

The gap is one of encoding convention, not of missing information. The same
identity exists on both sides: nwsviz places it inline on each variable (as GRIB2
attributes, including the raw `grib_section3` array for the grid), while cirrus
pushes it up to the group (HRRR) or root / nested group (radar) and encodes the
grid as a CF `spatial_ref` grid-mapping. Vertical levels differ the same way:
nwsviz names each coordinate by its interpreted surface type (`isobaric_surface`,
...), while cirrus keeps the raw GRIB2 fixed-surface primitives
(`scaled_value_of_first_fixed_surface`, ...). Neither difference changes what is
represented. Time is encoded the same way on both.
