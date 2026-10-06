"""Tests for the tocgrib2 GRIBIDS parm parser.

These encode the format facts the parser depends on. If a future NCEP change
breaks one of these assumptions, a test fails rather than the registry silently
gaining wrong headings.
"""

import pytest

from tocgrib2_parm import (
    PDT_4_0,
    PDT_4_8,
    WILDCARD,
    _expand_fortran_list,
    parse_line,
    parse_text,
    validate,
)

# A real line from grib2_awpgfs006.003 (GFS v16.3.24).
LINE_4_0 = (
    "&GRIBIDS DESC=' HGT      1000 mb ',WMOHEAD='YHPB99 KWBC',PDTN= 0 ,"
    "PDT=  3 5 2 0 96 0 0 1 6 100 0 100000 255 0 0 /"
)
# A real statistically-processed line, including Fortran repeat syntax.
LINE_4_8 = (
    "&GRIBIDS DESC=' A PCP    Surface ',WMOHEAD='YEPB98 KWBC',PDTN= 8 ,"
    "PDT=  1 8 2 0 96 0 0 1 0 1 0 0 255 0 0 6*-9999 1 0 1 2 1 6 255 0 /"
)
# A real line carrying EXTRACT, which must not disturb parsing.
LINE_EXTRACT = (
    "&GRIBIDS DESC=' U GRD    10 m above ground ',WMOHEAD='ZUBB98 KWBC',"
    "EXTRACT=.true.,PDTN= 0 ,PDT=  2 2 2 0 96 0 0 1 3 103 0 10 255 0 0 /"
)
# A real PDTN=8 line that supplies only the 15 template-4.0 slots. tocgrib2
# initialises PDT to -9999 before each read, so the rest are wildcards.
LINE_SHORT_PDT = (
    "&GRIBIDS DESC=' TMIN     2 m above ground ',WMOHEAD='ZTBB98 KWBC',PDTN= 8 ,"
    "PDT=  0 5 2 0 96 0 0 1 0 103 0 2 255 0 0 /"
)


class TestFortranList:
    def test_plain_integers(self):
        assert _expand_fortran_list("1 0 2 3") == [1, 0, 2, 3]

    def test_repeat_syntax_expands(self):
        assert _expand_fortran_list("6*-9999") == [-9999] * 6

    def test_repeat_mixed_with_plain(self):
        assert _expand_fortran_list("1 0 3*-9999 2") == [1, 0, -9999, -9999, -9999, 2]

    def test_commas_tolerated(self):
        assert _expand_fortran_list("1, 2, 3") == [1, 2, 3]

    def test_bad_token_raises(self):
        with pytest.raises(ValueError):
            _expand_fortran_list("1 abc 3")


class TestParseLine:
    def test_splits_wmohead_into_ttaaii_and_cccc(self):
        rec = parse_line(LINE_4_0, path="f", line_no=1)
        assert rec.ttaaii == "YHPB99"
        assert rec.cccc == "KWBC"
        assert rec.wmohead == "YHPB99 KWBC"

    def test_pdt_named_fields_for_template_4_0(self):
        f = parse_line(LINE_4_0).pdt_fields()
        assert f["parameterCategory"] == 3
        assert f["parameterNumber"] == 5
        assert f["typeOfGeneratingProcess"] == 2
        assert f["forecastTime"] == 6
        assert f["typeOfFirstFixedSurface"] == 100
        assert f["scaledValueOfFirstFixedSurface"] == 100000
        assert set(f) == set(PDT_4_0)

    def test_fortran_wildcards_become_none(self):
        f = parse_line(LINE_4_8).pdt_fields()
        # the 6*-9999 end-of-interval slots
        assert f["yearOfEndOfOverallTimeInterval"] is None
        assert f["secondOfEndOfOverallTimeInterval"] is None
        # the pinned statistical fields after them
        assert f["statisticalProcess"] == 1
        assert f["lengthOfTimeRange"] == 6
        assert set(f) == set(PDT_4_8)

    def test_short_pdt_pads_trailing_slots_as_wildcards(self):
        rec = parse_line(LINE_SHORT_PDT)
        assert rec.pdtn == 8
        assert len(rec.pdt) == 15  # raw list is short
        f = rec.pdt_fields()
        assert len(f) == len(PDT_4_8)  # but fields are padded
        assert f["typeOfFirstFixedSurface"] == 103
        assert f["statisticalProcess"] is None  # wildcard, not a real value

    def test_extract_keyword_does_not_break_pdt(self):
        rec = parse_line(LINE_EXTRACT)
        assert rec.ttaaii == "ZUBB98"
        f = rec.pdt_fields()
        assert f["parameterCategory"] == 2
        assert f["parameterNumber"] == 2
        assert f["scaledValueOfFirstFixedSurface"] == 10
        assert f["scaledValueOfSecondFixedSurface"] == 0

    def test_desc_and_abbreviation(self):
        assert parse_line(LINE_4_0).short_name() == "HGT"
        # two-word abbreviations must survive
        assert parse_line(LINE_4_8).short_name() == "A"

    def test_no_dscpl_or_gdt_in_gfs_parms(self):
        rec = parse_line(LINE_4_0)
        assert rec.discipline is None
        assert rec.gdtn is None
        assert rec.gdt == []

    @pytest.mark.parametrize(
        "line",
        ["", "   ", "! a comment", "something else entirely"],
    )
    def test_non_records_return_none(self, line):
        assert parse_line(line) is None

    def test_malformed_wmohead_raises(self):
        bad = "&GRIBIDS DESC=' X ',WMOHEAD='TOOLONGHEAD KWBC',PDTN= 0 ,PDT= 1 /"
        with pytest.raises(ValueError, match="unexpected WMOHEAD"):
            parse_line(bad, path="f", line_no=9)


class TestParseTextAndValidate:
    def test_parses_every_record_and_tracks_line_numbers(self):
        text = "\n".join([LINE_4_0, "! comment", LINE_4_8, "", LINE_EXTRACT])
        recs = parse_text(text, path="p")
        assert [r.line_no for r in recs] == [1, 3, 5]
        assert [r.ttaaii for r in recs] == ["YHPB99", "YEPB98", "ZUBB98"]

    def test_valid_records_report_no_problems(self):
        recs = parse_text("\n".join([LINE_4_0, LINE_4_8, LINE_SHORT_PDT]), path="p")
        assert validate(recs) == []

    def test_unknown_template_is_a_problem(self):
        line = "&GRIBIDS DESC=' X ',WMOHEAD='YXXX99 KWBC',PDTN= 99 ,PDT= 1 2 3 /"
        problems = validate(parse_text(line, path="p"))
        assert len(problems) == 1
        assert "unknown PDT template" in problems[0]

    def test_over_length_pdt_is_a_problem(self):
        over = " ".join(["1"] * (len(PDT_4_0) + 3))
        line = f"&GRIBIDS DESC=' X ',WMOHEAD='YXXX99 KWBC',PDTN= 0 ,PDT= {over} /"
        problems = validate(parse_text(line, path="p"))
        assert len(problems) == 1
        assert "template defines" in problems[0]

    def test_wildcard_sentinel_value(self):
        # Guard the constant the whole wildcard scheme rests on.
        assert WILDCARD == -9999
