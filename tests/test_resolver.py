"""Tests for the heading resolver.

The central guarantee under test: a heading is returned only when every field
traces to an authoritative record, and otherwise an Unresolved explains what is
missing. Nothing is defaulted, inferred or fabricated.

Most tests build a small synthetic registry so they are hermetic and fast.
``TestAgainstRealRegistry`` additionally pins two known-good results against the
committed registry, which is the regression guard that matters in practice.
"""

import gzip
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from wmo_header import (
    REGISTRY_DIR,
    Unresolved,
    WMOHeader,
    format_yygggg,
    identify_grid,
    load_registry,
    normalize_meaning,
    normalize_name,
    resolve,
    resolve_by_grib_identity,
    resolve_by_product_name,
)

T = datetime(2025, 9, 1, 12, 0, tzinfo=timezone.utc)

GRID_1P0 = {
    "name": "test_1p0deg",
    "expect_a1": "P",
    "a1_meaning_on388": "1.0x1.0 deg Global Lat/Lon grid",
    "section3": {
        "gridDefinitionTemplateNumber": 0,
        "Ni": 360,
        "Nj": 181,
        "lat_first": 90.0,
        "lon_first": 0.0,
        "di_deg": 1.0,
        "dj_deg": 1.0,
    },
}

UNASSIGNED_GRID_S3 = {
    "gridDefinitionTemplateNumber": 0,
    "Ni": 1440,
    "Nj": 721,
    "lat_first": 90.0,
    "lon_first": 0.0,
    "di_deg": 0.25,
    "dj_deg": 0.25,
}


def _write_source(d: Path, sid: str, manifest: dict, entries: list):
    if entries:
        p = d / f"{sid}.entries.jsonl.gz"
        with gzip.open(p, "wt", encoding="utf-8") as fh:
            for e in entries:
                fh.write(json.dumps(e) + "\n")
        manifest["entries_file"] = p.name
    (d / f"{sid}.manifest.json").write_text(json.dumps(manifest))


def _pdt(**over):
    base = {
        "parameterCategory": 3,
        "parameterNumber": 5,
        "typeOfGeneratingProcess": 2,
        "forecastTime": 6,
        "typeOfFirstFixedSurface": 100,
        "scaleFactorOfFirstFixedSurface": 0,
        "scaledValueOfFirstFixedSurface": 100000,
        "typeOfSecondFixedSurface": 255,
    }
    base.update(over)
    return base


@pytest.fixture
def registry(tmp_path):
    """A synthetic registry: one notice source, one parm source, code tables."""
    _write_source(
        tmp_path,
        "test-notice",
        {"source": {"id": "test-notice", "kind": "nws_notice", "cccc": "KWNR"}},
        [
            {
                "code": "YAUP06", "cccc": "KWNR", "domain": "CONUS", "a1": "U",
                "description": "MultiSensor_QPE_[01,24]H_Pass2", "status": "retained",
                "layout": "B", "product_names": ["MultiSensor_QPE_01H_Pass2",
                                                 "MultiSensor_QPE_24H_Pass2"],
                "usable_for_matching": True, "ambiguous": False,
                "source_id": "test-notice", "source_doc": "DOC", "source_raw": "raw",
            },
            {
                "code": "YAAP06", "cccc": "KWNR", "domain": "Alaska", "a1": "A",
                "description": "MultiSensor_QPE_[01,24]H_Pass2", "status": "retained",
                "layout": "B", "product_names": ["MultiSensor_QPE_24H_Pass2"],
                "usable_for_matching": True, "ambiguous": False,
                "source_id": "test-notice", "source_doc": "DOC", "source_raw": "raw",
            },
            {
                "code": "YAUS06", "cccc": "KWNR", "domain": "CONUS", "a1": "U",
                "description": "PrecipFlag", "status": "retained", "layout": "B",
                "product_names": [], "usable_for_matching": False,
                "ambiguous": True, "ambiguity_reason": "contradiction",
                "source_id": "test-notice", "source_doc": "DOC", "source_raw": "raw",
            },
        ],
    )
    _write_source(
        tmp_path,
        "test-parm",
        {
            "source": {
                "id": "test-parm",
                "kind": "tocgrib2_parm",
                "grid_binding": {"grids": [GRID_1P0]},
            }
        },
        [
            {
                "ttaaii": "YHPB99", "cccc": "KWBC", "grid": "test_1p0deg",
                "desc": "HGT      1000 mb", "pdtn": 0, "pdt": _pdt(),
                "source_id": "test-parm", "source_file": "parm_f006",
                "source_line": 1, "source_raw": "raw line",
            },
            {
                "ttaaii": "YHPB85", "cccc": "KWBC", "grid": "test_1p0deg",
                "desc": "HGT      850 mb", "pdtn": 0,
                "pdt": _pdt(scaledValueOfFirstFixedSurface=85000),
                "source_id": "test-parm", "source_file": "parm_f006",
                "source_line": 2, "source_raw": "raw line",
            },
        ],
    )
    _write_source(
        tmp_path,
        "test-codes",
        {
            "source": {"id": "test-codes", "kind": "wmo_code_table"},
            "code_tables": {
                "4.5": {"name": "typeOfFixedSurface",
                        "rows": [{"code": 100, "meaning": "Isobaric surface"}]},
            },
        },
        [],
    )
    return load_registry(tmp_path, auto_build=False)


class TestNormalization:
    def test_product_names_compare_case_and_separator_insensitively(self):
        assert normalize_name("MultiSensor_QPE_24H_Pass2") == "multisensorqpe24hpass2"
        assert normalize_name("multisensor-qpe-24h-pass2") == normalize_name(
            "MultiSensor_QPE_24H_Pass2"
        )

    def test_meaning_drops_unit_suffix_and_folds_wording(self):
        # Archives render table meanings with a unit suffix and 'Specified'.
        assert normalize_meaning("Specified Height Level Above Ground (m)") == (
            normalize_meaning("Specific height level above ground")
        )


class TestTimeFormatting:
    def test_yygggg_is_day_hour_minute_utc(self):
        assert format_yygggg(datetime(2025, 9, 30, 18, 0, tzinfo=timezone.utc)) == "301800"

    def test_naive_datetime_treated_as_utc(self):
        assert format_yygggg(datetime(2025, 9, 1, 6, 5)) == "010605"

    @pytest.mark.parametrize("bad", [None, "not a time", object()])
    def test_unusable_time_returns_none_never_now(self, bad):
        # Falling back to the current time would make the heading inaccurate.
        assert format_yygggg(bad) is None


class TestGridIdentification:
    def test_matches_assigned_grid(self, registry):
        assert identify_grid(GRID_1P0["section3"], registry) == "test_1p0deg"

    def test_unassigned_grid_returns_none(self, registry):
        assert identify_grid(UNASSIGNED_GRID_S3, registry) is None

    def test_resolution_difference_is_not_a_match(self, registry):
        near = dict(GRID_1P0["section3"], di_deg=1.25)
        assert identify_grid(near, registry) is None


class TestNameMatching:
    def test_resolves_with_domain(self, registry):
        r = resolve_by_product_name("MultiSensor_QPE_24H_Pass2", "CONUS", T, registry)
        assert isinstance(r, WMOHeader)
        assert r.heading == "YAUP06 KWNR 011200"
        assert r.source_id == "test-notice"

    def test_domain_selects_between_codes(self, registry):
        r = resolve_by_product_name("MultiSensor_QPE_24H_Pass2", "Alaska", T, registry)
        assert r.ttaaii == "YAAP06"

    def test_unknown_product_unresolved(self, registry):
        r = resolve_by_product_name("NotAProduct", "CONUS", T, registry)
        assert isinstance(r, Unresolved)
        assert "no authoritative NWS notice" in r.reasons[0]

    def test_product_excluded_by_conflict_is_unresolved(self, registry):
        # PrecipFlag's code was marked unusable at build time.
        r = resolve_by_product_name("PrecipFlag", "CONUS", T, registry)
        assert isinstance(r, Unresolved)

    def test_domain_without_assignment_unresolved(self, registry):
        r = resolve_by_product_name("MultiSensor_QPE_24H_Pass2", "Guam", T, registry)
        assert isinstance(r, Unresolved)
        assert "not for domain" in r.reasons[0]

    def test_undetermined_domain_with_multiple_options_unresolved(self, registry):
        r = resolve_by_product_name("MultiSensor_QPE_24H_Pass2", None, T, registry)
        assert isinstance(r, Unresolved)
        assert "domain could not be determined" in r.reasons[0]

    def test_missing_time_unresolved(self, registry):
        r = resolve_by_product_name("MultiSensor_QPE_24H_Pass2", "CONUS", None, registry)
        assert isinstance(r, Unresolved)
        assert "reference time" in r.reasons[0]


class TestIdentityMatching:
    def _identity(self, **over):
        ident = {
            "section3": GRID_1P0["section3"],
            "pdtn": 0,
            "pdt": {
                "typeOfGeneratingProcess": 2,
                "typeOfFirstFixedSurface": 100,
                "forecastTime": 6,
                "_levelPhysical": 100000.0,
            },
        }
        ident.update(over)
        return ident

    def test_resolves_and_cites_parm_line(self, registry):
        r = resolve_by_grib_identity("HGT", self._identity(), T, registry)
        assert isinstance(r, WMOHeader)
        assert r.heading == "YHPB99 KWBC 011200"
        assert r.source_ref == "parm_f006:1"

    def test_level_discriminates_between_headings(self, registry):
        ident = self._identity()
        ident["pdt"]["_levelPhysical"] = 85000.0
        r = resolve_by_grib_identity("HGT", ident, T, registry)
        assert r.ttaaii == "YHPB85"

    def test_native_grid_falls_back_to_parameter_level(self, registry):
        # The archive's 0.25-deg global grid is not a headered grid, but the
        # parameter IS headered (on the assigned global grid), so we report that
        # header flagged as a parameter-level match with the provenance caveat.
        r = resolve_by_grib_identity(
            "HGT", self._identity(section3=UNASSIGNED_GRID_S3), T, registry
        )
        assert isinstance(r, WMOHeader)
        assert r.ttaaii == "YHPB99"
        assert r.match == "parameter"
        assert r.assigned_grid == "test_1p0deg"
        assert "1440x721" in r.archive_grid
        assert r.caveat and "native file grid" in r.caveat

    def test_native_grid_unresolved_when_fallback_disabled(self, registry):
        r = resolve_by_grib_identity(
            "HGT", self._identity(section3=UNASSIGNED_GRID_S3), T, registry,
            allow_parameter_fallback=False,
        )
        assert isinstance(r, Unresolved)
        assert "not an assigned" in r.reasons[0]

    def test_parameter_never_headered_stays_unresolved(self, registry):
        # If no assigned grid carries the parameter at all, even the fallback
        # must refuse -- it does not invent a header.
        ident = self._identity(section3=UNASSIGNED_GRID_S3)
        ident["pdt"]["_levelPhysical"] = 12345.0  # a level no record has
        r = resolve_by_grib_identity("HGT", ident, T, registry)
        assert isinstance(r, Unresolved)

    def test_exact_grid_still_preferred_over_fallback(self, registry):
        # When the archive IS on the assigned grid, it must be an exact match,
        # not a parameter-level one.
        r = resolve_by_grib_identity("HGT", self._identity(), T, registry)
        assert isinstance(r, WMOHeader)
        assert r.match == "exact"

    def test_missing_grid_unresolved(self, registry):
        r = resolve_by_grib_identity("HGT", self._identity(section3=None), T, registry)
        assert isinstance(r, Unresolved)
        assert "grid could not be determined" in r.reasons[0]

    def test_unknown_abbreviation_unresolved(self, registry):
        r = resolve_by_grib_identity("NOSUCH", self._identity(), T, registry)
        assert isinstance(r, Unresolved)
        assert "abbreviation" in r.reasons[0]

    def test_ambiguous_level_yields_unresolved_not_a_guess(self, registry):
        # Without a level, both HGT records match; the resolver must refuse
        # rather than pick one.
        ident = self._identity()
        del ident["pdt"]["_levelPhysical"]
        r = resolve_by_grib_identity("HGT", ident, T, registry)
        assert isinstance(r, Unresolved)
        assert "matches 2 different headers" in r.reasons[0]

    def test_disagreeing_field_yields_no_match(self, registry):
        # A forecast hour no record has must not match on any grid, exact or
        # fallback: the result is Unresolved, never a wrong header.
        ident = self._identity()
        ident["pdt"]["forecastTime"] = 999
        r = resolve_by_grib_identity("HGT", ident, T, registry)
        assert isinstance(r, Unresolved)

    def test_wildcard_fields_do_not_block_a_match(self, registry):
        # The parm records leave scaledValueOfSecondFixedSurface unpinned (None in
        # some records); evidence we lack must not count as disagreement.
        ident = self._identity()
        ident["pdt"].pop("typeOfGeneratingProcess")
        r = resolve_by_grib_identity("HGT", ident, T, registry)
        assert isinstance(r, WMOHeader)


class TestCombinedResolve:
    def test_name_match_wins_when_available(self, registry):
        r = resolve("MultiSensor_QPE_24H_Pass2", T, registry, domain="CONUS")
        assert isinstance(r, WMOHeader)
        assert r.ttaaii == "YAUP06"

    def test_falls_through_to_identity(self, registry):
        r = resolve(
            "HGT",
            T,
            registry,
            identity={
                "section3": GRID_1P0["section3"],
                "pdtn": 0,
                "pdt": {"typeOfGeneratingProcess": 2, "typeOfFirstFixedSurface": 100,
                        "forecastTime": 6, "_levelPhysical": 100000.0},
            },
        )
        assert isinstance(r, WMOHeader)
        assert r.ttaaii == "YHPB99"

    def test_reports_reasons_from_every_route(self, registry):
        r = resolve("Unknown", T, registry, domain="CONUS", identity={
            "section3": UNASSIGNED_GRID_S3, "pdtn": 0, "pdt": {}})
        assert isinstance(r, Unresolved)
        assert any("[name match]" in x for x in r.reasons)
        assert any("[identity match]" in x for x in r.reasons)

    def test_notes_absent_identity_evidence(self, registry):
        r = resolve("HGT", T, registry, domain="CONUS")
        assert isinstance(r, Unresolved)
        assert any("no GRIB2 identity evidence" in x for x in r.reasons)


@pytest.mark.skipif(
    not (REGISTRY_DIR / "nws-mrms-sbn.manifest.json").exists(),
    reason="built registry not present; run build_registry.py",
)
class TestAgainstRealRegistry:
    """Pins known-good results against the committed registry."""

    @pytest.fixture(scope="class")
    @classmethod
    def real(cls):
        return load_registry(auto_build=False)

    def test_mrms_multisensor_pass2_conus(self, real):
        r = resolve_by_product_name(
            "MultiSensor_QPE_24H_Pass2", "CONUS",
            datetime(2025, 9, 30, 18, 0, tzinfo=timezone.utc), real,
        )
        assert isinstance(r, WMOHeader)
        assert r.heading == "YAUP06 KWNR 301800"

    def test_parameter_identity_is_grib2io_sourced(self, real):
        # grib2io is authoritative for identity; the vast majority of
        # abbreviations must come from it, with only the documented gap set
        # backfilled from the parm files.
        from collections import Counter

        from wmo_header import GRIB2IO_ABBREV_GAPS

        counts = Counter(real.abbrev_source.values())
        assert counts["grib2io"] > 1000
        gaps = {a for a, s in real.abbrev_source.items() if s == "parm-gap"}
        assert gaps <= set(GRIB2IO_ABBREV_GAPS)

    def test_gap_abbreviations_are_covered(self, real):
        # The parm-gap abbreviations grib2io lacks must still be identifiable, or
        # we would lose real headered GFS products (categorical precip, etc.).
        for abbrev in ("cfrzr", "cicep", "crain", "csnow"):
            assert abbrev in real.abbrev_param or abbrev in real.abbrev_ambiguous

    def test_cccc_validation_classifies_correctly(self, real):
        # XR-09 directory: WFO codes confirmed, national centres known-absent,
        # bogus codes flagged.
        assert real.validate_cccc("KBOU") == "wfo"
        assert real.validate_cccc("KWNR") == "national"   # MRMS centre
        assert real.validate_cccc("KWBC") == "national"   # NCEP centre
        assert real.validate_cccc("KZZZ") == "unlisted"

    def test_mrms_header_cccc_status_is_national(self, real):
        r = resolve(
            "MultiSensor_QPE_24H_Pass2",
            datetime(2025, 9, 30, 18, 0, tzinfo=timezone.utc),
            real, domain="CONUS",
        )
        assert isinstance(r, WMOHeader)
        assert r.cccc_status == "national"   # KWNR: valid, not a WFO node

    def test_xr04_awips_xref_loaded(self, real):
        # XR-04 available as a reference table (AWIPS NNN -> WMO components).
        assert real.awips_xref.get("AFD", {}).get("tt") == ["FX"]
        assert real.awips_xref.get("DAA", {}).get("tt") == ["SD"]  # radar QPE

    def test_rtma_analysis_header_composed(self, real):
        from wmo_header import resolve_analysis_header

        r = resolve_analysis_header("TMP", "rtma|2p5|conus", T, real)
        assert isinstance(r, WMOHeader)
        assert r.ttaaii == "LTIA98"   # L + T(temp) + I(2.5km CONUS) + A98
        assert r.cccc == "KWBR"

    def test_urma_analysis_header_uses_q(self, real):
        from wmo_header import resolve_analysis_header

        r = resolve_analysis_header("TMP", "urma|2p5|conus", T, real)
        assert isinstance(r, WMOHeader)
        assert r.ttaaii == "LTQA98"  # Q = 2.5km CONUS URMA

    def test_rtma_wind_family_all_map_to_N(self, real):
        from wmo_header import resolve_analysis_header

        for sn in ("WIND", "WDIR", "GUST"):
            r = resolve_analysis_header(sn, "rtma|2p5|conus", T, real)
            assert isinstance(r, WMOHeader) and r.ttaaii == "LNIA98"

    def test_rtma_precip_and_unlisted_params_unresolved(self, real):
        from wmo_header import resolve_analysis_header

        # APCP deliberately excluded (TIN uses fixed A1, no URMA LEQA98 stated).
        assert isinstance(
            resolve_analysis_header("APCP", "urma|2p5|conus", T, real), Unresolved
        )
        # SPFH / CEIL are not in the TIN tables at all.
        assert isinstance(
            resolve_analysis_header("SPFH", "rtma|2p5|conus", T, real), Unresolved
        )

    def test_rtma_unknown_domain_unresolved(self, real):
        from wmo_header import resolve_analysis_header

        assert isinstance(
            resolve_analysis_header("TMP", "rtma|2p5|antarctica", T, real), Unresolved
        )

    @pytest.mark.parametrize(
        "product,domain,expected",
        [
            ("PrecipRate", "CONUS", "YAUP02"),
            ("PrecipRate", "Alaska", "YAAP02"),
            ("PrecipRate", "Hawaii", "YAHP02"),
            ("MultiSensor_QPE_01H_Pass1", "CONUS", "YAUP04"),
            ("MergedBaseReflectivityQC", "CONUS", "YAUQ01"),
            ("ReflectivityAtLowestAltitude", "CONUS", "YAUS22"),
        ],
    )
    def test_known_mrms_assignments(self, real, product, domain, expected):
        r = resolve_by_product_name(product, domain, T, real)
        assert isinstance(r, WMOHeader), getattr(r, "reasons", None)
        assert r.ttaaii == expected

    def test_precipflag_refused_due_to_source_contradiction(self, real):
        # The v12.2 supplemental attaches YAUS06 to PrecipFlag, contradicting
        # three other notices. The build marks it unusable, so this must not
        # resolve.
        r = resolve_by_product_name("PrecipFlag", "CONUS", T, real)
        assert isinstance(r, Unresolved)

    def test_gfs_1p0deg_hgt_1000mb(self, real):
        r = resolve_by_grib_identity(
            "HGT",
            {
                "section3": {
                    "gridDefinitionTemplateNumber": 0, "Ni": 360, "Nj": 181,
                    "lat_first": 90.0, "lon_first": 0.0,
                    "di_deg": 1.0, "dj_deg": 1.0,
                },
                "pdtn": 0,
                "pdt": {
                    "typeOfGeneratingProcess": 2, "typeOfFirstFixedSurface": 100,
                    "forecastTime": 6, "_levelPhysical": 100000.0,
                },
            },
            datetime(2021, 1, 1, 6, 0, tzinfo=timezone.utc),
            real,
        )
        assert isinstance(r, WMOHeader), getattr(r, "reasons", None)
        assert r.ttaaii == "YHPB99"
        assert r.cccc == "KWBC"

    def test_gfs_0p25deg_resolves_at_parameter_level(self, real):
        # The archived 0.25-deg grid is the native file grid (no header of its
        # own), but HGT@1000mb IS headered on the assigned 1-deg global grid, so
        # we report that header flagged as parameter-level with the caveat.
        r = resolve_by_grib_identity(
            "HGT",
            {"section3": UNASSIGNED_GRID_S3, "pdtn": 0,
             "pdt": {"typeOfGeneratingProcess": 2, "typeOfFirstFixedSurface": 100,
                     "forecastTime": 6, "_levelPhysical": 100000.0}},
            datetime(2021, 1, 1, 6, 0, tzinfo=timezone.utc), real,
        )
        assert isinstance(r, WMOHeader), getattr(r, "reasons", None)
        assert r.ttaaii == "YHPB99"          # the 1-deg global grid's header
        assert r.match == "parameter"
        assert r.caveat and "native file grid" in r.caveat

    def test_gfs_0p25deg_exact_only_is_unresolved(self, real):
        # With the fallback disabled, the native grid correctly has no exact header.
        r = resolve_by_grib_identity(
            "HGT",
            {"section3": UNASSIGNED_GRID_S3, "pdtn": 0,
             "pdt": {"typeOfGeneratingProcess": 2, "typeOfFirstFixedSurface": 100,
                     "forecastTime": 6, "_levelPhysical": 100000.0}},
            T, real, allow_parameter_fallback=False,
        )
        assert isinstance(r, Unresolved)
