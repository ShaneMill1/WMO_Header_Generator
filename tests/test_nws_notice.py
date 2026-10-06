"""Tests for the NWS notice parsers (MRMS header assignments).

The fixtures below mirror the real document structures, including the two
defects the parser must refuse to guess past: a code the source contradicts
itself on, and annotations scoped by prose the parser cannot interpret.
"""

import pytest

from nws_notice import (
    expand_product_patterns,
    extract_cccc,
    find_conflicts,
    mark_conflicts,
    parse_header_table,
    parse_html_product_annotations,
)

# Layout A, in the shape pypdf yields: codes and descriptions run together, and
# section headings establish SBN status.
NOTICE_TEXT = """
Table 2. WMO Headers and Product Names for MRMS Products Currently on the
MRMS SBN to be Retained
=====================================================================
CONUS Products to be Retained on the MRMS SBN
=====================================================================
YAUP02   Precipitation Rate
YAUQ01   Mosaic Base Reflectivity (optimal method)
=====================================================================
Alaska Products to be Added to the MRMS SBN:
=====================================================================
YAAP02   PrecipRate
Table 3. WMO Headers and Product Names for MRMS Products Proposed for
Removal from the MRMS SBN
============================================================
CONUS Products to be Removed from the MRMS SBN
============================================================
YAUS13   Vertically Integrated Liquid (VIL)
CCCC = KWNR
"""

# Layout B: product name and its WMO annotation inside one table cell.
HTML_SINGLE = """
<table><tr>
  <td>
    <p>MultiSensor_QPE_[01,03,06,12,24,48,72]H_Pass2</p>
    <p>*On SBN with WMO codes YAUP06 (CONUS), YAAP06 (Alaska), YAHP06 (Hawaii)*</p>
  </td>
  <td><p>GRIB2</p></td>
</tr></table>
"""

# Layout B with several products in one cell: the code cannot be tied to one.
HTML_MULTI_PRODUCT = """
<table><tr>
  <td>
    <p>RadarOnly_QPE_15M</p>
    <p>RadarOnly_QPE_[01,03,06,12,24,48,72]H</p>
    <p>RadarOnly_QPE_Since12Z</p>
    <p>*01-72H QPE on SBN with WMO codes YAUP03 (CONUS)*</p>
  </td>
</tr></table>
"""

# Layout B where the annotation is scoped by prose ("60 and 1440min").
HTML_SCOPED = """
<table><tr>
  <td>
    <p>RotationTrack[30,60,120,360,1440]min</p>
    <p>*60 and 1440min on SBN with WMO code YAUS04 (CONUS)*</p>
  </td>
</tr></table>
"""

# The real v12.2 defect: YAUS06 attached to PrecipFlag, though it designates
# Mid-Level Rotation Tracks elsewhere.
HTML_CONTRADICTION = """
<table>
<tr><td>
  <p>RotationTrackML[30,60,120,360,1440]min</p>
  <p>*On SBN with WMO code YAUS06 (CONUS)*</p>
</td></tr>
<tr><td>
  <p>PrecipFlag</p>
  <p>*On SBN with WMO code YAUS06 (CONUS)*</p>
</td></tr>
</table>
"""


class TestLayoutA:
    def test_extracts_codes_and_descriptions(self):
        by_code = {e.code: e for e in parse_header_table(NOTICE_TEXT, "DOC")}
        assert by_code["YAUP02"].description == "Precipitation Rate"
        assert by_code["YAUQ01"].description == "Mosaic Base Reflectivity (optimal method)"

    def test_description_stops_at_structural_boundary(self):
        # YAUS13's description must not swallow the trailing "CCCC = KWNR".
        by_code = {e.code: e for e in parse_header_table(NOTICE_TEXT, "DOC")}
        assert by_code["YAUS13"].description == "Vertically Integrated Liquid (VIL)"

    def test_domain_derived_from_a1_character(self):
        by_code = {e.code: e for e in parse_header_table(NOTICE_TEXT, "DOC")}
        assert by_code["YAUP02"].a1 == "U"
        assert by_code["YAUP02"].domain == "CONUS"
        assert by_code["YAAP02"].domain == "Alaska"

    def test_status_follows_section_heading(self):
        by_code = {e.code: e for e in parse_header_table(NOTICE_TEXT, "DOC")}
        assert by_code["YAUP02"].status == "retained"
        assert by_code["YAAP02"].status == "added"
        assert by_code["YAUS13"].status == "removed"

    def test_cccc_declaration(self):
        assert extract_cccc(NOTICE_TEXT) == "KWNR"
        assert extract_cccc("no declaration here") is None


class TestBracketExpansion:
    def test_expands_interval_list(self):
        got = expand_product_patterns("MultiSensor_QPE_[01,03,24]H_Pass2")
        assert got == [
            "MultiSensor_QPE_01H_Pass2",
            "MultiSensor_QPE_03H_Pass2",
            "MultiSensor_QPE_24H_Pass2",
        ]

    def test_plain_name_passes_through(self):
        assert expand_product_patterns("PrecipRate") == ["PrecipRate"]

    def test_empty_yields_nothing(self):
        assert expand_product_patterns("   ") == []


class TestLayoutB:
    def test_ties_product_to_codes_per_domain(self):
        entries = parse_html_product_annotations(HTML_SINGLE, "DOC")
        by_domain = {e.domain: e for e in entries}
        assert set(by_domain) == {"CONUS", "Alaska", "Hawaii"}
        assert by_domain["CONUS"].code == "YAUP06"
        assert by_domain["Hawaii"].code == "YAHP06"
        assert all(not e.ambiguous for e in entries)

    def test_usable_entry_expands_to_concrete_names(self):
        conus = [
            e
            for e in parse_html_product_annotations(HTML_SINGLE, "DOC")
            if e.domain == "CONUS"
        ][0]
        assert "MultiSensor_QPE_24H_Pass2" in conus.product_names
        assert len(conus.product_names) == 7

    def test_multi_product_cell_is_ambiguous(self):
        entries = parse_html_product_annotations(HTML_MULTI_PRODUCT, "DOC")
        assert entries and all(e.ambiguous for e in entries)
        assert all(e.product_names == [] for e in entries)
        assert "3 products" in entries[0].ambiguity_reason

    def test_scoped_annotation_is_ambiguous(self):
        entries = parse_html_product_annotations(HTML_SCOPED, "DOC")
        assert entries and all(e.ambiguous for e in entries)
        assert "scoped by" in entries[0].ambiguity_reason
        assert entries[0].annotation_qualifier == "60 and 1440min"

    def test_domain_disagreement_raises(self):
        # Code implies Alaska (A1='A') but the document labels it CONUS.
        bad = """
        <table><tr><td>
          <p>PrecipRate</p>
          <p>*On SBN with WMO code YAAP02 (CONUS)*</p>
        </td></tr></table>
        """
        with pytest.raises(ValueError, match="implies domain"):
            parse_html_product_annotations(bad, "DOC")


class TestConflicts:
    def test_contradiction_detected_and_classified(self):
        entries = parse_html_product_annotations(HTML_CONTRADICTION, "DOC")
        conflicts = find_conflicts(entries)
        assert ("YAUS06", "CONUS") in conflicts
        assert conflicts[("YAUS06", "CONUS")]["kind"] == "contradiction"

    def test_contradiction_makes_code_unusable(self):
        entries = mark_conflicts(parse_html_product_annotations(HTML_CONTRADICTION, "DOC"))
        assert all(e.ambiguous for e in entries)
        assert all(e.product_names == [] for e in entries)

    def test_variant_ambiguity_classified_separately(self):
        entries = parse_html_product_annotations(HTML_MULTI_PRODUCT, "DOC")
        conflicts = find_conflicts(entries)
        assert conflicts[("YAUP03", "CONUS")]["kind"] == "variant_ambiguity"

    def test_bracket_expansion_is_not_a_conflict(self):
        # One pattern expanding to 7 names is the assignment working as intended.
        entries = parse_html_product_annotations(HTML_SINGLE, "DOC")
        assert find_conflicts(entries) == {}
        mark_conflicts(entries)
        conus = [e for e in entries if e.domain == "CONUS"][0]
        assert not conus.ambiguous
        assert "MultiSensor_QPE_24H_Pass2" in conus.product_names
