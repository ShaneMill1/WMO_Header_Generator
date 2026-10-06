"""Parsers for NWS notice documents that publish MRMS WMO header assignments.

MRMS headers are not applied by ``tocgrib2`` (its headers are stamped by the
IDP application), so there is no operational parm file to read. The
authoritative public record is the set of NWS notices -- Technical
Implementation Notices, Public Information Statements and Service Change
Notice supplementals -- which publish the product/header tables.

Two distinct layouts appear in those documents, and both are parsed here:

**Layout A -- header table.** A WMO code followed by its product description,
grouped under section headings that state whether the products are being
added to, retained on, or removed from the SBN::

    CONUS Products to be Retained on the MRMS SBN
    YAUP02   Precipitation Rate
    YAUP03   Radar Precipitation Accumulation

**Layout B -- product annotation.** A product name (often with a bracketed
list of accumulation intervals) annotated with the code(s) it is disseminated
under, per domain::

    MultiSensor_QPE_[01,03,06,12,24,48,72]H_Pass2
    *On SBN with WMO codes YAUP06 (CONUS), YAAP06 (Alaska), YAHP06 (Hawaii)*

Layout B matters for matching real data: it carries the concrete product names
(``MultiSensor_QPE_24H_Pass2``) that appear as ``short_name`` in the archives,
whereas Layout A carries prose descriptions ("Multi-sensor QPE Pass2").

Parsing notes:

* PDF text extraction merges lines, so codes are located by splitting the whole
  text on the code pattern rather than by reading line by line. A description
  is therefore the text between one code and the next, truncated at the first
  structural boundary (table caption, rule line, etc.).
* The A1 (third) character of the code encodes the domain, so the domain is
  derived from the code itself and cross-checked against any domain label
  stated alongside it.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Optional

# WMO code as used by MRMS: T1='Y', T2='A', A1=domain, A2 + ii.
CODE_RE = re.compile(r"\b(Y[A-Z][A-Z][A-Z]\d{2})\b")

# A1 (3rd character) -> domain label. Sourced from the domain groupings stated
# in the NWS notices themselves (CONUS/Alaska/Hawaii sections).
A1_DOMAIN = {
    "U": "CONUS",
    "A": "Alaska",
    "H": "Hawaii",
    "G": "Guam",
    "C": "Caribbean",
}

# Section headings that establish SBN status for the codes that follow.
STATUS_MARKERS = [
    (re.compile(r"to\s+be\s+(?:added|Added)|[Pp]roposed\s+for\s+[Aa]ddition", re.I), "added"),
    (re.compile(r"to\s+be\s+[Rr]etained|[Cc]urrently\s+on\s+the\s+MRMS\s+SBN", re.I), "retained"),
    (re.compile(r"to\s+be\s+[Rr]emoved|[Pp]roposed\s+for\s+[Rr]emoval|removed\s+from\s+(?:the\s+)?MRMS\s+SBN", re.I), "removed"),
]

# Boundaries at which a description must stop (structural text, not description).
DESC_BOUNDARY = re.compile(
    r"(Table\s+\d+\.|={5,}|-{5,}|CCCC\s*=|NWS\s+is\s+also|In\s+addition\s+to|"
    r"Domain\s+WMO|pg\.\s*\d+\s+of\s+\d+)",
    re.I,
)

# Layout B: "WMO code YAUQ01" / "WMO codes YAUP04 (CONUS), YAAP04 (Alaska)".
WMO_CODE_ANNOTATION = re.compile(
    r"WMO\s+codes?\s+((?:Y[A-Z]{3}\d{2}\s*(?:\([^)]*\))?\s*,?\s*)+)", re.I
)
CODE_WITH_DOMAIN = re.compile(r"(Y[A-Z]{3}\d{2})\s*(?:\(([^)]*)\))?")

# An MRMS product name as written in the notices: an identifier that may carry a
# bracketed interval list, e.g. MultiSensor_QPE_[01,03,24]H_Pass2,
# RadarOnly_QPE_15M,[01,03]H, PrecipRate, MergedBaseReflectivityQC,
# RotationTrackML[30,60]min
PRODUCT_NAME_RE = re.compile(
    r"^[A-Za-z][A-Za-z0-9_]*(?:\[[0-9,\s]+\]|[,_A-Za-z0-9])*$"
)


@dataclass
class NoticeEntry:
    """One (code, domain) allocation extracted from a notice document."""

    code: str                     # TTAAii, e.g. 'YAUP06'
    domain: str                   # A1-derived label, e.g. 'CONUS'
    a1: str                       # A1 character, e.g. 'U'
    description: str = ""         # verbatim description / product text
    status: Optional[str] = None  # 'added' | 'retained' | 'removed' | None
    layout: str = "A"             # which layout produced this entry
    product_names: list = field(default_factory=list)  # concrete names (Layout B)
    # Ambiguity: set when the document does not unambiguously tie this code to a
    # single product. Ambiguous entries are recorded for review but must not be
    # used to match data, because picking one candidate could yield a wrong
    # header.
    ambiguous: bool = False
    ambiguity_reason: str = ""
    candidates: list = field(default_factory=list)
    annotation_qualifier: str = ""
    # provenance
    doc_id: str = ""
    char_offset: int = 0
    raw: str = ""


def _truncate_description(text: str) -> str:
    """Collapse whitespace and cut at the first structural boundary."""
    m = DESC_BOUNDARY.search(text)
    if m:
        text = text[: m.start()]
    return " ".join(text.split()).strip(" .,;:")


def _status_regions(text: str) -> list:
    """Return sorted (offset, status) markers found in the document."""
    marks = []
    for pattern, status in STATUS_MARKERS:
        for m in pattern.finditer(text):
            marks.append((m.start(), status))
    marks.sort()
    return marks


def _status_at(marks: list, offset: int) -> Optional[str]:
    """Status established by the nearest preceding marker."""
    status = None
    for pos, st in marks:
        if pos <= offset:
            status = st
        else:
            break
    return status


def expand_product_patterns(text: str) -> list:
    """Expand bracketed interval lists into concrete product names.

    ``MultiSensor_QPE_[01,03,24]H_Pass2`` ->
    ``['MultiSensor_QPE_01H_Pass2', 'MultiSensor_QPE_03H_Pass2',
       'MultiSensor_QPE_24H_Pass2']``

    Text without brackets is returned unchanged (as a single-element list).
    """
    m = re.search(r"\[([0-9,\s]+)\]", text)
    if not m:
        cleaned = text.strip()
        return [cleaned] if cleaned else []
    values = [v.strip() for v in m.group(1).split(",") if v.strip()]
    return [text[: m.start()] + v + text[m.end():] for v in values]


def parse_header_table(text: str, doc_id: str) -> list:
    """Layout A: extract (code, description, status) triples from a notice."""
    marks = _status_regions(text)
    entries: list = []
    parts = CODE_RE.split(text)
    # parts = [pre, code1, between1, code2, between2, ...]
    offset = len(parts[0])
    for i in range(1, len(parts), 2):
        code = parts[i]
        between = parts[i + 1] if i + 1 < len(parts) else ""
        a1 = code[2]
        entries.append(
            NoticeEntry(
                code=code,
                a1=a1,
                domain=A1_DOMAIN.get(a1, f"unknown({a1})"),
                description=_truncate_description(between),
                status=_status_at(marks, offset),
                layout="A",
                doc_id=doc_id,
                char_offset=offset,
                raw=" ".join((code + between[:160]).split()),
            )
        )
        offset += len(code) + len(between)
    return entries


def _cell_paragraphs(cell_html: str) -> list:
    """Plain-text paragraphs within one HTML table cell."""
    # Treat <p> and <br> as paragraph separators, then strip remaining tags.
    s = re.sub(r"<\s*br\s*/?\s*>", "\n", cell_html, flags=re.I)
    s = re.sub(r"<\s*/\s*p\s*>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s).replace("\xa0", " ")
    return [" ".join(p.split()) for p in s.split("\n") if p.strip()]


def parse_html_product_annotations(html_text: str, doc_id: str) -> list:
    """Layout B over structured HTML: product name and WMO code in one cell.

    The notices lay each product out as a table cell containing the product name
    in one paragraph and its dissemination note in another::

        <td>
          <p>MultiSensor_QPE_[01,03,06,12,24,48,72]H_Pass2</p>
          <p>*On SBN with WMO codes YAUP06 (CONUS), YAAP06 (Alaska), ...*</p>
        </td>

    Scoping the association to a single cell removes the ambiguity that makes
    the same extraction unreliable on the PDF renderings, where text wrapping
    can split a product name across lines.
    """
    entries: list = []
    for cell in re.findall(r"<td\b[^>]*>(.*?)</td>", html_text, flags=re.I | re.S):
        paras = _cell_paragraphs(cell)
        ann_idx = [i for i, p in enumerate(paras) if WMO_CODE_ANNOTATION.search(p)]
        if not ann_idx:
            continue
        for i in ann_idx:
            m = WMO_CODE_ANNOTATION.search(paras[i])
            if not m:
                continue

            # All product-shaped paragraphs preceding the annotation in this cell.
            candidates = [
                p.strip().strip("*")
                for p in paras[:i]
                if PRODUCT_NAME_RE.match(p.strip().strip("*"))
            ]

            # A qualifier is any substantive text before "on SBN ..." in the
            # annotation, e.g. "*01-72H QPE on SBN with WMO codes YAUP03 ...*".
            # It narrows which of the cell's products the code applies to.
            qualifier = ""
            q = re.split(r"\bon\s+SBN\b", paras[i], maxsplit=1, flags=re.I)[0]
            q = q.strip().strip("*").strip()
            if q and not re.fullmatch(r"[\W_]*", q):
                qualifier = q

            ambiguous = False
            reason = ""
            product = candidates[-1] if candidates else None
            if not candidates:
                ambiguous, reason = True, "no product name found in the cell"
            elif len(candidates) > 1:
                ambiguous = True
                reason = (
                    f"cell lists {len(candidates)} products "
                    f"({', '.join(candidates)}) so the code cannot be tied to one"
                )
            elif qualifier:
                ambiguous = True
                reason = (
                    f"annotation is scoped by {qualifier!r}, which this parser "
                    f"does not interpret"
                )

            for cm in CODE_WITH_DOMAIN.finditer(m.group(1)):
                code = cm.group(1)
                stated_domain = (cm.group(2) or "").strip() or None
                a1 = code[2]
                derived = A1_DOMAIN.get(a1, f"unknown({a1})")
                # Cross-check the document's own domain label against the domain
                # implied by the code's A1 character.
                if stated_domain and stated_domain.lower() not in derived.lower():
                    raise ValueError(
                        f"{doc_id}: code {code} implies domain {derived!r} from "
                        f"A1={a1!r} but document states {stated_domain!r}"
                    )
                entries.append(
                    NoticeEntry(
                        code=code,
                        a1=a1,
                        domain=derived,
                        description=product or "",
                        status="retained",
                        layout="B",
                        product_names=(
                            [] if ambiguous or not product
                            else expand_product_patterns(product)
                        ),
                        ambiguous=ambiguous,
                        ambiguity_reason=reason,
                        candidates=candidates,
                        annotation_qualifier=qualifier,
                        doc_id=doc_id,
                        char_offset=0,
                        raw=" ".join(paras[: i + 1])[-240:],
                    )
                )
    return entries


def find_conflicts(entries: list) -> dict:
    """Detect codes associated with more than one product, and classify how.

    A WMO code designates one product per domain, so a code seen against several
    products is unusable either way. But the two causes differ in severity and
    are reported separately:

    ``contradiction``
        The products belong to different product lines (e.g. ``PrecipFlag`` and
        ``RotationTrackML``). The document disagrees with itself -- in practice a
        typo -- and a human must resolve it.

    ``variant_ambiguity``
        The products are variants within one line (e.g. ``RadarOnly_QPE_15M``,
        ``RadarOnly_QPE_[01..72]H``, ``RadarOnly_QPE_Since12Z``). The document is
        internally consistent but does not say which variants the code covers,
        often because the annotation is scoped in prose.

    Both unusable mappings and the candidate lists of already-ambiguous entries
    are considered, so a bad code is caught even when one of its appearances was
    flagged ambiguous for some other reason.

    Returns ``{(code, domain): {"kind": ..., "reason": ...}}``.
    """
    # Compare the *source patterns*, not their expansions. One pattern such as
    # ``MultiSensor_QPE_[01,03,...]H_Pass2`` legitimately expands to many concrete
    # names under a single code; that is the assignment working as intended, not
    # competing products. Ambiguity means the document offers several distinct
    # patterns for one code.
    lines: dict = {}
    for e in entries:
        if e.candidates:
            patterns = list(e.candidates)
        elif e.description and e.layout == "B":
            patterns = [e.description]
        else:
            continue
        key = (e.code, e.domain)
        for pat in patterns:
            lines.setdefault(key, {}).setdefault(_product_line(pat), set()).add(pat)

    conflicts: dict = {}
    for key, by_line in lines.items():
        all_names = sorted({n for names in by_line.values() for n in names})
        if len(all_names) < 2:
            continue
        if len(by_line) > 1:
            kind = "contradiction"
            reason = (
                f"code {key[0]} ({key[1]}) is associated with products from "
                f"{len(by_line)} different product lines: {', '.join(all_names)}"
            )
        else:
            kind = "variant_ambiguity"
            reason = (
                f"code {key[0]} ({key[1]}) is associated with "
                f"{len(all_names)} variants of one product line "
                f"({', '.join(all_names)}) without stating which it covers"
            )
        conflicts[key] = {"kind": kind, "reason": reason}
    return conflicts


def mark_conflicts(entries: list) -> list:
    """Flag every entry whose (code, domain) is conflicted as unusable."""
    conflicts = find_conflicts(entries)
    for e in entries:
        c = conflicts.get((e.code, e.domain))
        if c:
            e.ambiguous = True
            e.ambiguity_reason = (
                (e.ambiguity_reason + "; " if e.ambiguity_reason else "") + c["reason"]
            )
            e.product_names = []
    return entries


def _product_line(name: str) -> str:
    """Coarse product-line key: the leading identifier before any digit/bracket.

    Groups interval variants of one product together
    (``RadarOnly_QPE_15M`` and ``RadarOnly_QPE_[01,03]H`` share a line) while
    keeping genuinely different products apart (``PrecipFlag`` versus
    ``RotationTrackML``).
    """
    s = name.lower()
    m = re.match(r"^([a-z_]+)", s)
    head = m.group(1) if m else s
    parts = [p for p in head.split("_") if p]
    return "_".join(parts[:2]) if len(parts) > 1 else (parts[0] if parts else s)


def parse_notice(text: str, doc_id: str) -> list:
    """Parse the header-table layout (Layout A) from a notice's text.

    For the product-annotation layout use :func:`parse_html_product_annotations`
    against the HTML rendering, which is unambiguous.
    """
    return parse_header_table(text, doc_id)


def extract_cccc(text: str) -> Optional[str]:
    """Extract an explicit 'CCCC = XXXX' declaration if the document states one."""
    m = re.search(r"CCCC\s*=\s*([A-Z]{4})", text)
    return m.group(1) if m else None
