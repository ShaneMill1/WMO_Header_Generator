"""Tests for GRIB2 identity extraction from archived metadata.

The level tests matter most: picking the wrong level coordinate would inject a
wrong level into the identity and could match a wrong heading, so the level must
come only from the authoritative ``grib_name`` marker.
"""

import numpy as np
import pytest
import xarray as xr

from grib_identity import (
    extract_identity,
    levels_close,
    message_pdt,
    parse_section3,
    physical_level,
)

# Real Section 3 arrays from the archives.
S3_MRMS = [0, 24500000, 0, 0, 0, 2, 1, 6367470, 1, 6378160, 1, 6356775,
           7000, 3500, 1, 1000000, 54995000, 230005000, 48, 20005001,
           299994998, 10000, 10000, 0]
S3_GFS = [0, 1038240, 0, 0, 0, 6, 0, 0, 0, 0, 0, 0, 1440, 721, 0, -1,
          90000000, 0, 48, -90000000, 359750000, 250000, 250000, 0]


class FakeRegistry:
    """Minimal stand-in exposing only the code-table lookup the extractor uses."""

    def __init__(self, mapping=None):
        # Note: an explicit empty mapping must stay empty, so test `is None`
        # rather than truthiness.
        self.mapping = {
            ("4.3", "Forecast"): 2,
            ("4.3", "Observation"): 0,
            ("4.5", "Isobaric Surface (Pa)"): 100,
            ("4.5", "Specified Height Level Above Ground (m)"): 103,
            ("4.5", "Specific Altitude Above Mean Sea Level (m)"): 102,
            ("4.10", "Average"): 0,
            ("4.10", "Accumulation"): 1,
        } if mapping is None else mapping

    def lookup_code(self, table, meaning):
        return self.mapping.get((table, meaning))


class TestParseSection3:
    def test_mrms_grid_fields(self):
        g = parse_section3(S3_MRMS)
        assert g["gridDefinitionTemplateNumber"] == 0
        assert (g["Ni"], g["Nj"]) == (7000, 3500)
        # Cross-checked against the store's own scalar attributes.
        assert g["lat_first"] == pytest.approx(54.995)
        assert g["lon_first"] == pytest.approx(230.005)
        assert g["di_deg"] == pytest.approx(0.01)

    def test_gfs_grid_fields(self):
        g = parse_section3(S3_GFS)
        assert (g["Ni"], g["Nj"]) == (1440, 721)
        assert g["lat_first"] == pytest.approx(90.0)
        assert g["di_deg"] == pytest.approx(0.25)

    def test_accepts_json_string(self):
        assert parse_section3(str(S3_GFS))["Ni"] == 1440

    @pytest.mark.parametrize("bad", [None, "not json", [1, 2, 3], ["a"] * 30])
    def test_unusable_input_returns_none(self, bad):
        assert parse_section3(bad) is None


class TestPhysicalLevel:
    @pytest.mark.parametrize(
        "scaled,factor,expected",
        [
            (100000, 0, 100000.0),   # 1000 hPa in Pa
            (85000, 0, 85000.0),     # 850 hPa
            (2, 0, 2.0),             # 2 m above ground
            (0, 0, 0.0),             # surface
            (15, 1, 1.5),            # scale factor applied
        ],
    )
    def test_scaling(self, scaled, factor, expected):
        assert physical_level(scaled, factor) == pytest.approx(expected)

    def test_missing_scale_factor_treated_as_zero(self):
        assert physical_level(500, None) == pytest.approx(500.0)

    def test_missing_value_is_none(self):
        assert physical_level(None, 0) is None

    def test_levels_close_absorbs_float_error(self):
        assert levels_close(85000.0, 85000.0)
        assert levels_close(1.5, 1.5000000001)
        assert not levels_close(85000.0, 85001.0)
        assert not levels_close(None, 85000.0)


def _mrms_like():
    """A dataset shaped like the MRMS archive: scalar level, many times."""
    times = np.array(["2025-09-01T00:00", "2025-09-01T06:00"], dtype="datetime64[ns]")
    da = xr.DataArray(
        np.zeros((2, 2, 2), dtype="float32"),
        dims=("time", "y", "x"),
        coords={"time": times},
        name="precipitation_accumulation_24_hour",
        attrs={
            "short_name": "MultiSensor_QPE_24H_Pass2",
            "grib_section3": str(S3_MRMS),
            "product_definition_template_number":
                "Analysis or forecast at a horizontal level ... (see Template 4.0)",
            "type_of_generating_process": "Observation",
            "type_of_first_fixed_surface": "Specific Altitude Above Mean Sea Level (m)",
        },
    )
    ds = da.to_dataset()
    ds = ds.assign_coords(
        specific_altitude_above_mean_sea_level=xr.DataArray(
            0.0,
            attrs={"units": "m",
                   "grib_name": "['valueOfFirstFixedSurface', 'typeOfFirstFixedSurface']"},
        ),
        lead_time=xr.DataArray(np.timedelta64(0, "h"), attrs={"grib_name": "leadTime"}),
    )
    return ds[da.name], ds


def _gfs_like():
    """A dataset shaped like GFS: level is a DIMENSION, lead parallel to time."""
    times = np.array(["2021-01-01T06:00", "2021-01-01T12:00"], dtype="datetime64[ns]")
    levels = np.array([85000.0, 100000.0])
    da = xr.DataArray(
        np.zeros((2, 2, 2, 2), dtype="float32"),
        dims=("time", "isobaric_surface", "latitude", "longitude"),
        coords={"time": times, "isobaric_surface": levels},
        name="height_isobaric_surface",
        attrs={
            "short_name": "HGT",
            "grib_section3": str(S3_GFS),
            "product_definition_template_number":
                "Analysis or forecast at a horizontal level ... (see Template 4.0)",
            "type_of_generating_process": "Forecast",
            "type_of_first_fixed_surface": "Isobaric Surface (Pa)",
        },
    )
    ds = da.to_dataset()
    ds["isobaric_surface"].attrs.update(
        units="Pa",
        grib_name="['valueOfFirstFixedSurface', 'typeOfFirstFixedSurface']",
    )
    ds = ds.assign_coords(
        lead_time=xr.DataArray(
            np.array([6, 12], dtype="timedelta64[h]"),
            dims=("time",),
            attrs={"grib_name": "leadTime"},
        )
    )
    return ds[da.name], ds


class TestExtractIdentity:
    def test_scalar_level_yields_one_level(self):
        da, ds = _mrms_like()
        ident = extract_identity(da, ds, FakeRegistry())
        assert ident.short_name == "MultiSensor_QPE_24H_Pass2"
        assert ident.pdtn == 0
        assert ident.levels == [0.0]
        assert ident.level_units == "m"
        assert len(ident.times) == 2

    def test_dimension_level_yields_every_level(self):
        # The regression this guards: a level dimension must fan out, not be
        # ignored, because each level is a separate message with its own heading.
        da, ds = _gfs_like()
        ident = extract_identity(da, ds, FakeRegistry())
        assert ident.levels == [85000.0, 100000.0]
        assert ident.level_coord == "isobaric_surface"
        assert ident.level_units == "Pa"

    def test_prose_translated_to_grib2_codes(self):
        da, ds = _gfs_like()
        ident = extract_identity(da, ds, FakeRegistry())
        assert ident.pdt["typeOfGeneratingProcess"] == 2
        assert ident.pdt["typeOfFirstFixedSurface"] == 100

    def test_untranslatable_prose_is_omitted_and_noted(self):
        da, ds = _gfs_like()
        ident = extract_identity(da, ds, FakeRegistry(mapping={}))
        assert "typeOfGeneratingProcess" not in ident.pdt
        assert any("did not map" in n for n in ident.notes)

    def test_lead_hours_parallel_to_time(self):
        da, ds = _gfs_like()
        ident = extract_identity(da, ds, FakeRegistry())
        assert ident.lead_hours == [6.0, 12.0]

    def test_scalar_lead_broadcast_over_times(self):
        da, ds = _mrms_like()
        ident = extract_identity(da, ds, FakeRegistry())
        assert ident.lead_hours == [0.0, 0.0]

    def test_level_requires_authoritative_grib_name(self):
        # A plausibly-named coordinate without the grib_name marker must be
        # ignored rather than guessed at.
        da, ds = _mrms_like()
        del ds["specific_altitude_above_mean_sea_level"].attrs["grib_name"]
        ident = extract_identity(da, ds, FakeRegistry())
        assert ident.levels == []
        assert any("valueOfFirstFixedSurface" in n for n in ident.notes)


class TestMessagePdt:
    def test_level_carried_as_physical_value(self):
        da, ds = _gfs_like()
        ident = extract_identity(da, ds, FakeRegistry())
        pdt = message_pdt(ident, 85000.0, 6.0)
        assert pdt["_levelPhysical"] == 85000.0
        assert pdt["forecastTime"] == 6
        assert pdt["indicatorOfUnitOfTimeRange"] == 1

    def test_fractional_lead_hour_omitted(self):
        # A non-integral hour cannot be expressed as forecastTime in hours, so it
        # is left out rather than rounded.
        da, ds = _gfs_like()
        ident = extract_identity(da, ds, FakeRegistry())
        pdt = message_pdt(ident, 85000.0, 1.5)
        assert "forecastTime" not in pdt

    def test_absent_level_omitted(self):
        da, ds = _gfs_like()
        ident = extract_identity(da, ds, FakeRegistry())
        assert "_levelPhysical" not in message_pdt(ident, None, 6.0)
