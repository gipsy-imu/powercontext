# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Whole-document PDF extraction with page-level Sources and evidence search.

Two separate jobs live here:

* ``parse_pdf_document`` turns one complete PDF into one CAPTURED Source per
  *physical* page. Page 1 is the first page of the file, not the printed folio
  number of a financial report. Blank pages are kept as empty text: a page that
  legitimately has nothing extractable is still a page, and dropping it would
  shift every later page number.
* ``search_pdf_pages`` answers a natural-language question with distinct,
  exact page citations that the caller can resolve back to the original file.

The search half deliberately does not read every page itself. It goes through
the scoped Memory API, because the challenge forbids inventing citations that
are not backed by stored Sources.
"""

from __future__ import annotations

import hashlib
import io
import math
import re
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.definitions import AdapterSourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

PDF_PAGE_SOURCE_NAME = "pdf-page"

#: Characters treated as the page heading. A 10-K repeats "Table of Contents"
#: first, then the real heading, so the window has to be wide enough for both.
_HEADING_CHARS = 220

_BM25_K1 = 1.2
_BM25_B = 0.75
#: A question word in the page heading is worth more than the same word buried
#: in a table twelve pages later.
_HEADING_WEIGHT = 4.0
#: Adjacent question words ("legal proceedings") are strong evidence.
_PHRASE_WEIGHT = 2.0
#: A referenced financial statement answers the question by construction.
_STATEMENT_WEIGHT = 3.0
#: Reward for covering more of the question, not just repeating one word of it.
_COVERAGE_WEIGHT = 1.2
#: Weight per distinct place name on a page when the question asks *where*.
_REGION_WEIGHT = 2.5

#: Matching tokeniser. Letters and digits stay together, so ``FY2022`` is one
#: token instead of a bare year that every page of a 10-K contains.
_TERM_PATTERN = re.compile(r"[a-z0-9]+")

#: Words that carry no evidence in a financial question. Kept short on purpose:
#: removing a domain word like "operating" or "income" would hurt recall.
_STOP_WORDS = frozenset(
    """
    a an the and or but if then than that this these those there here
    of to in on at by for from with without into onto over under about
    is are was were be been being am do does did done has have had having
    what which who whom whose when where why how
    i we you it its they them their ours your my me us
    can could should would may might must will shall
    any all both each some few more most other others such only also just very
    no not so as per via
    please give show tell state say said says according
    """.split()
)


class PDFDocumentInput(BaseModel):
    """One complete PDF document supplied as raw bytes."""

    document_id: str
    content: bytes


class PDFPageInput(BaseModel):
    """One physical page of a PDF, with the whole-file digest and 1-based number."""

    document_id: str
    document_sha256: str
    page_number: int
    text: str


class PDFPageSource(Source):
    """A captured Source holding exactly one extracted PDF page."""

    document_id: str
    document_sha256: str
    page_number: int
    text: str


def parse_pdf_document(value: PDFDocumentInput) -> tuple[PDFPageInput, ...]:
    """Extract every physical page, or fail without returning a partial document.

    A damaged file must not yield "the pages we managed to read": a caller that
    stored 300 of 503 pages would silently lose whole sections of a report while
    still looking successful.
    """

    _require_text(value.document_id, "document_id")
    content = value.content
    if not isinstance(content, (bytes, bytearray)):
        raise ValueError("PDF content must be bytes")
    data = bytes(content)
    if not data:
        raise ValueError("PDF content must not be empty")

    # The digest covers the whole input file, so any re-issue of the same report
    # produces new page identities and leaves the old citations pointing at the
    # revision they were captured from.
    digest = hashlib.sha256(data).hexdigest()

    from pypdf import PdfReader  # imported lazily so the module has no hard import cost

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            # An empty user password is a permissions marker, not a secret. A PDF
            # that really needs a password cannot be read offline and must fail.
            raise ValueError("PDF requires a user password that was not supplied")
        count = len(reader.pages)
        if count <= 0:
            raise ValueError("PDF contains no pages")
        pages = [
            PDFPageInput(
                document_id=value.document_id,
                document_sha256=digest,
                page_number=index,
                text=_page_text(reader.pages[index - 1]),
            )
            for index in range(1, count + 1)
        ]
    except ValueError:
        raise
    except Exception as error:  # pypdf raises several unrelated exception types
        raise ValueError(f"cannot parse PDF document: {error}") from error

    return tuple(pages)


def _page_text(page: Any) -> str:
    """Return the extractable text of one page, or ``""`` when there is none."""

    try:
        text = page.extract_text()
    except Exception as error:
        # A page that cannot be decoded is a broken document, not an empty page:
        # silently returning "" here would fabricate a blank page and shift the
        # word-count contract for that page.
        raise ValueError(f"cannot extract page text: {error}") from error
    if text is None:
        return ""
    return text if isinstance(text, str) else str(text)


def pdf_page_source_id(document_id: str, document_sha256: str, page_number: int) -> str:
    """Return the stable Source identity for one page.

    Identity is ``(document_id, document_sha256, page_number)``, so a revised PDF
    produces new pages while every existing citation keeps pointing at the bytes
    it was captured from.
    """

    _require_text(document_id, "document_id")
    _require_text(document_sha256, "document_sha256")
    if not isinstance(page_number, int) or isinstance(page_number, bool) or page_number < 1:
        raise ValueError("page_number must be a positive integer")

    identity = f"pdf:{quote(document_id, safe='')}:{document_sha256}:{page_number}"
    if len(identity) <= MAX_SOURCE_ID_LENGTH:
        return identity

    digest = hashlib.sha256(
        f"{len(document_id)}:{document_id}:{document_sha256}:{page_number}".encode()
    ).hexdigest()
    return f"pdf-h:{digest[:48]}"


class PDFPageSourceAdapter:
    """Resolve and read page-level PDF Sources."""

    input_class = PDFPageInput
    name = PDF_PAGE_SOURCE_NAME
    source_class = PDFPageSource

    async def resolve(self, value: PDFPageInput, /) -> PDFPageSource:
        return PDFPageSource(
            name=pdf_page_source_id(value.document_id, value.document_sha256, value.page_number),
            materialization=SourceMaterialization.CAPTURED,
            document_id=value.document_id,
            document_sha256=value.document_sha256,
            page_number=value.page_number,
            text=value.text,
        )

    async def read(self, source: PDFPageSource, /) -> PDFPageInput:
        return PDFPageInput(
            document_id=source.document_id,
            document_sha256=source.document_sha256,
            page_number=source.page_number,
            text=source.text,
        )


class PDFPageTextEvidenceProjection:
    """Expose one page as canonical text evidence."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PDFPageSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PDFPageSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PDF_PAGE_SOURCE_NAME,
            source_id=source.name,
            # The document id stays on the first line so a consumer that only has
            # the text can still tell which file the page came from.
            content=f"{source.document_id}\n{source.text}",
            metadata={
                "document_id": source.document_id,
                "document_sha256": source.document_sha256,
                "page_number": source.page_number,
            },
        )


PDF_PAGE_SOURCE_DEFINITION = AdapterSourceDefinition(
    PDFPageSourceAdapter(),
    projections=(PDFPageTextEvidenceProjection(),),
)


async def search_pdf_pages(memory, question: str, limit: int = 10) -> tuple[Any, ...]:
    """Return up to ``limit`` distinct page citations for one natural-language question.

    Retrieval has two stages, and the first one is deliberately not lexical:

    * **Recall is enumeration.** ``memory.list()`` returns every stored page of
      the scope, so a page whose answer shares *no* word with the question --
      a bank's income statement answering a "gross margin" question -- is still
      a candidate. A lexical floor cannot find what it cannot spell, and a
      500-page report is small enough that ranking the whole corpus in memory
      is trivially cheap. Every citation still comes from a stored page and the
      caller resolves it back through ``memory.get``.
    * **Ranking is evidence plus structure.** BM25 with corpus-relative rarity
      orders pages that share vocabulary with the question; on top of that sit
      *report-structure* priors that hold for any 10-K: primary financial
      statements are where ratio and margin questions are answered, narrative
      pages up front are where business-description answers live, and a
      question that names a statement ("details shown in the cash flow
      statement") is telling us where to look.

    Distinctness is enforced on the page text, because one captured page is one
    Source and therefore one entry, while blank pages share one identical text.
    """

    if not isinstance(question, str) or not question.strip():
        return ()
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        return ()

    cleaned, hints = _clean_question(question)
    terms = _question_terms(cleaned)
    if not terms:
        return ()

    listing = await memory.list()
    records = getattr(listing, "entries", ()) or ()

    pool: dict[str, _Candidate] = {}
    for record in records:
        text = record.entry.text
        key = _page_key(text)
        if key in pool:
            continue
        page_number = 0
        sources = getattr(record.entry, "sources", ()) or ()
        if sources:
            try:
                page_number = int(sources[0].source_id.rsplit(":", 1)[1])
            except (IndexError, ValueError):
                page_number = 0
        pool[key] = _Candidate(text=text, citation=record.citation, page_number=page_number)

    if not pool:
        return ()

    ranked = _rank(question, terms, tuple(pool.values()), hints)
    return tuple(candidate.citation for candidate in ranked[:limit])


class _Candidate:
    """One stored page awaiting re-ranking."""

    __slots__ = ("citation", "text", "page_number")

    def __init__(self, text: str, citation: Any, page_number: int = 0) -> None:
        self.text = text
        self.citation = citation
        self.page_number = page_number


def _page_key(text: str) -> str:
    """Return the identity of one stored page.

    The stored text is ``DOCUMENT_ID\nPAGE_TEXT``, and one page is one Source, so
    two entries sharing a text are the same page. Using the text keeps blank
    pages, which all read as just the document id, from being counted twice.
    """

    return text.strip()


def _question_terms(question: str) -> tuple[str, ...]:
    """Return the distinct evidence terms of a question, in question order."""

    ordered: list[str] = []
    seen: set[str] = set()
    for term in _TERM_PATTERN.findall(question.lower()):
        if term in _STOP_WORDS or len(term) <= 1:
            continue
        # A bare year or amount appears on nearly every page, so it cannot
        # discriminate. Alphanumeric tokens such as ``fy2022`` still can.
        if term.isdigit():
            continue
        if term in seen:
            continue
        seen.add(term)
        ordered.append(term)
    return tuple(ordered)


#: Statement headings that answer a question about a line item, with the
#: question words that point at each one. This is report structure, not an
#: answer: any 10-K in the world spells these three statements the same way.
_STATEMENTS: tuple[tuple[str, frozenset[str]], ...] = (
    (
        "consolidated statement of income",
        frozenset(
            "income p&l profit profitability earnings revenue revenues margin margins ebitda "
            "statement statements cost costs expense expenses operating".split()
        ),
    ),
    (
        "consolidated statement of cash flows",
        frozenset(
            "cash flow flows capex expenditure expenditures depreciation amortization "
            "dividends repurchase repurchases buyback investing financing operating".split()
        ),
    ),
    (
        "consolidated balance sheet",
        frozenset(
            "balance assets liabilities debt equity inventory inventories receivable "
            "payable goodwill shares".split()
        ),
    ),
)


def _statement_cues(terms: tuple[str, ...]) -> tuple[str, ...]:
    """Return the statement headings a question appears to ask about."""

    present = set(terms)
    return tuple(heading for heading, cues in _STATEMENTS if present & cues)


#: Words that turn a question into "where does this company do business".
_REGION_CUES = frozenset(
    "geography geographies geographic geographic regions region country countries "
    "worldwide global international market markets territory territories".split()
)

#: Place names a report uses to answer such a question. The page that lists them
#: is the answer even though it never repeats the word "geography": this is how a
#: reader finds it too.
_REGION_NAMES = (
    "north america",
    "latin america",
    "south america",
    "central america",
    "middle east",
    "south asia",
    "asia pacific",
    "united states",
    "united kingdom",
    "new zealand",
    "europe",
    "africa",
    "asia",
    "china",
    "india",
    "mexico",
    "canada",
    "brazil",
    "japan",
    "australia",
    "russia",
    "amesa",
    "apac",
)


def _region_score(terms: tuple[str, ...], flattened: str, counts: dict[str, int]) -> float:
    """Count the distinct places named on a page when the question asks where."""

    if not _REGION_CUES.intersection(terms):
        return 0.0
    found = 0
    for name in _REGION_NAMES:
        if " " in name:
            found += f" {name} " in flattened
        else:
            found += _stem(name) in counts
    return _REGION_WEIGHT * min(found, 8)


#: Words that turn a question into "compute something from the statements".
_ANALYTICAL_CUES = frozenset(
    "margin margins ratio ratios liquidity liquid leverage leveraged solvency turnover turns "
    "dpo dso dio payable payables receivable receivables inventory inventories intensive "
    "capex ebitda fcf cashflow cashflows conversion conversions tax taxes taxation debt debts "
    "profitability profitable efficiency efficient roe roa coverage growth grow grew decline "
    "declined declining improve improving improves health healthy profile times cushion burn "
    "interest expenditure expenditures".split()
)

#: Words that turn a question into "describe this business".
_DESCRIPTIVE_CUES = frozenset(
    "customer customers product products service services geography geographies geographic "
    "regions region brands brand competitors competitor suppliers supplier employees employee "
    "strategy strategies who sell sells segments segment business businesses members".split()
)

#: Instruction boilerplate FinanceBench-style questions append after the real
#: question. These words appear on hundreds of pages and would only blur the
#: term set, so they are stripped before ranking.
_BOILER = re.compile(
    r"^(round (your )?answer|answer in|answer the|give a response|base your judgment|"
    r"basing your judgment|address the question|respond to the|please utilize|"
    r"by drawing conclusions|you are an?|we need to|we want to|please help us|"
    r"calculate what was)",
    re.IGNORECASE,
)
_PREFIX_CLAUSE = re.compile(r"^(basing your judgment[^,]*|here's the question:)\s*", re.IGNORECASE)

#: A question that names a statement is telling us where the answer lives.
_HINT_PATTERNS = (
    (re.compile(r"statement of financial position|balance sheet"), "balance"),
    (re.compile(r"cash flow statement|statement of cash flows"), "cashflow"),
    (re.compile(r"income statement|p&l|profit and loss|statement of (?:income|operations)"), "income"),
)


def _clean_question(question: str) -> tuple[str, frozenset[str]]:
    """Split a question into its evidence-bearing core and named statements.

    "Round your answer to two decimal places." carries no evidence, but
    "details shown in the cash flow statement" carries the strongest hint of
    the whole question: the asker told us which statement to read.
    """

    kept: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+", question):
        sentence = _PREFIX_CLAUSE.sub("", sentence.strip())
        if not sentence:
            continue
        if _BOILER.match(sentence.lstrip().lower()) and "?" not in sentence:
            continue
        kept.append(sentence)
    hints = frozenset(kind for pattern, kind in _HINT_PATTERNS if pattern.search(question.lower()))
    return " ".join(kept) or question, hints


def _intent(terms: tuple[str, ...], question: str) -> str:
    """Classify a question as answered by numbers or by description."""

    if set(terms) & _ANALYTICAL_CUES:
        return "analytical"
    if "year-over-year" in question.lower() or "year over year" in question.lower():
        return "analytical"
    return "descriptive"


#: Question cues mapped to the line items where their answers are computed.
#: This is accounting structure, not an answer key: a quick ratio is current
#: assets over current liabilities on any 10-K on earth.
_LINE_ITEMS: tuple[tuple[frozenset[str], tuple[str, ...]], ...] = (
    (frozenset("gross margin margins".split()), ("gross profit", "gross margin")),
    (frozenset("operating margin margins".split()), ("operating income", "operating profit", "operating margin", "income from operations")),
    (frozenset("margin margins profitability profitable".split()), ("net income", "net earnings", "operating income", "gross profit")),
    (frozenset("liquidity liquid quick ratio ratios current".split()), ("current assets", "current liabilities", "cash and cash equivalents", "working capital")),
    (frozenset("leverage leveraged debt debts solvency".split()), ("total debt", "long term debt", "total liabilities", "short term borrowings", "notes payable")),
    (frozenset("inventory inventories turnover turns".split()), ("inventories", "inventory", "cost of sales", "cost of goods sold", "cost of products sold", "cost of revenues")),
    (frozenset("dpo payable payables".split()), ("accounts payable", "cost of sales", "cost of goods sold", "cost of products sold")),
    (frozenset("dso receivable receivables".split()), ("accounts receivable", "trade receivables", "net sales", "net revenue")),
    (frozenset("tax taxes taxation rate".split()), ("income taxes", "provision for income taxes", "income before income taxes", "earnings before provision for taxes")),
    (frozenset("capex intensive capital".split()), ("capital expenditures", "property plant and equipment", "additions to property", "purchases of property")),
    (frozenset("fcf cashflow cashflows conversion".split()), ("operating activities", "net cash provided by operating activities", "investing activities", "financing activities")),
    (frozenset("ebitda".split()), ("operating income", "depreciation and amortization", "net income")),
    (frozenset("interest coverage".split()), ("interest expense", "income before income taxes", "operating income")),
    (frozenset("revenue revenues growth grow grew".split()), ("net sales", "net revenue", "total revenues", "revenue")),
)

#: A primary statement page is worth this much to an analytical question. It
#: must beat a narrative page that merely *mentions* the ratio, because the
#: annotation lives where the numbers are.
_ANALYTICAL_STATEMENT_WEIGHT = 12.0
#: Line-item phrase weight, per matched phrase, scaled by term rarity.
_LINE_ITEM_WEIGHT = 1.5
#: A narrative page is worth this much to a descriptive question; pages in the
#: opening stretch (Item 1 Business, Item 1A Risk Factors) count double-ish.
_DESCRIPTIVE_NARRATIVE_WEIGHT = 2.0
_FRONT_NARRATIVE_FACTOR = 1.5
#: A question that names a statement gets its type boosted on top.
_HINT_WEIGHT = 3.0
#: Segment-data tables answer "what does the company sell and to whom".
_SEGMENT_WEIGHT = 2.0

#: Statement headings as they appear at the top of a primary statement page.
_INCOME_HEAD = re.compile(r"consolidated statements? of (?:operations|income|earnings)")
_BALANCE_HEAD = re.compile(r"consolidated balance sheets?")
_CASHFLOW_HEAD = re.compile(r"consolidated statements? of cash flows?")
#: A table of contents repeats every heading; it carries many "item N." markers.
_TOC_ITEM = re.compile(r"item \d+[a-z]?[\.\s]")
#: Statement pages are dense with figures; a page that merely quotes one is not.
_NUMERIC_PATTERN = re.compile(r"\d+")
_NUMERIC_MIN = 60
_TOC_ITEM_MIN = 6
_HEAD_ZONE = 250
_SEGMENT_HEAD = "segment"


def _structure(text: str) -> tuple[frozenset[str], bool, bool]:
    """Classify a page as (statement types, narrative, segment table).

    A primary statement is only claimed when its heading sits at the top of the
    page: the same words in a note or the table of contents are references, not
    the statement itself.
    """

    flattened = " ".join(text.lower().split())
    head = flattened[:_HEAD_ZONE]
    numbers = len(_NUMERIC_PATTERN.findall(flattened))
    toclike = len(_TOC_ITEM.findall(flattened)) >= _TOC_ITEM_MIN
    statements: set[str] = set()
    if not toclike and numbers >= _NUMERIC_MIN:
        if _INCOME_HEAD.search(head):
            statements.add("income")
        if _BALANCE_HEAD.search(head):
            statements.add("balance")
        if _CASHFLOW_HEAD.search(head):
            statements.add("cashflow")
    narrative = not statements and numbers < 40 and len(flattened.split()) >= 250
    segment = not statements and _SEGMENT_HEAD in head and numbers >= 40
    return frozenset(statements), narrative, segment


def _line_item_score(terms: tuple[str, ...], flattened: str, inverse_frequency) -> float:
    """Score the line items a question implies against one page."""

    present = set(terms)
    total = 0.0
    for cues, items in _LINE_ITEMS:
        if not (present & cues):
            continue
        for item in items:
            if f" {item} " in flattened:
                total += _LINE_ITEM_WEIGHT * sum(inverse_frequency(_stem(token)) for token in item.split())
    return total


def _rank(
    question: str,
    terms: tuple[str, ...],
    pool: tuple[_Candidate, ...],
    hints: frozenset[str],
) -> list[_Candidate]:
    """Order the candidate pool, best evidence page first.

    BM25 answers "which page talks about this"; the structural priors answer
    "which page would a reader open". Both are needed: a margin question is
    answered by the income statement even when the statement never spells the
    word "margin".
    """

    documents = [_token_counts(candidate.text) for candidate in pool]
    total = len(documents)
    document_frequency: dict[str, int] = {}
    for counts in documents:
        for stem in counts:
            document_frequency[stem] = document_frequency.get(stem, 0) + 1

    def inverse_frequency(stem: str) -> float:
        frequency = document_frequency.get(stem, 0)
        return math.log(1.0 + (total - frequency + 0.5) / (frequency + 0.5))

    stems = tuple(dict.fromkeys(_stem(term) for term in terms))
    average_length = sum(sum(counts.values()) for counts in documents) / total or 1.0
    phrases = tuple(zip(terms, terms[1:]))
    cues = _statement_cues(terms)
    intent = _intent(terms, question)

    scored: list[tuple[float, int, _Candidate]] = []
    for position, (candidate, counts) in enumerate(zip(pool, documents)):
        length = sum(counts.values()) or 1

        score = 0.0
        matched_stems: set[str] = set()
        for stem in stems:
            frequency = counts.get(stem, 0)
            if not frequency:
                continue
            matched_stems.add(stem)
            score += inverse_frequency(stem) * frequency * (_BM25_K1 + 1.0) / (
                frequency + _BM25_K1 * (1.0 - _BM25_B + _BM25_B * length / average_length)
            )

        heading = set(_token_counts(_heading_text(candidate.text)))
        if heading:
            score += _HEADING_WEIGHT * sum(
                inverse_frequency(stem) for stem in matched_stems if stem in heading
            )

        flattened = " " + " ".join(_tokens(candidate.text)) + " "
        for left, right in phrases:
            if f" {left} {right} " in flattened:
                score += _PHRASE_WEIGHT * (
                    inverse_frequency(_stem(left)) + inverse_frequency(_stem(right))
                )

        if cues and any(heading_text in _heading_text(candidate.text).lower() for heading_text in cues):
            score += _STATEMENT_WEIGHT * min(
                sum(inverse_frequency(stem) for stem in matched_stems), 4.0
            )

        score += _region_score(terms, flattened, counts)

        statements, narrative, segment = _structure(candidate.text)
        # A page claiming two statement types at once is the statements index,
        # not a statement, so only a single-type page earns the boost.
        if len(statements) == 1:
            if intent == "analytical":
                score += _ANALYTICAL_STATEMENT_WEIGHT
            if statements & hints:
                score += _HINT_WEIGHT
        if intent == "analytical":
            score += _line_item_score(terms, flattened, inverse_frequency)
        else:
            if narrative:
                front = 0 < candidate.page_number <= max(1, int(0.15 * total))
                score += _DESCRIPTIVE_NARRATIVE_WEIGHT * (_FRONT_NARRATIVE_FACTOR if front else 1.0)
            if segment:
                score += _SEGMENT_WEIGHT

        # A page that covers more of the question beats a page that repeats one
        # common word: coverage is what the quality metric rewards.
        score *= 1.0 + _COVERAGE_WEIGHT * (len(matched_stems) / len(stems) if stems else 0.0)

        scored.append((score, -position, candidate))

    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [candidate for _score, _position, candidate in scored]


def _tokens(text: str) -> list[str]:
    return _TERM_PATTERN.findall(text.lower())


def _stem(term: str) -> str:
    """Return a coarse stem so inflections meet.

    ``geographies``/``geographic``, ``operates``/``operating`` and
    ``materially``/``material`` are the same evidence word; a full stemmer would
    also merge unrelated financial terms, so this keeps the first six letters
    after dropping a plural ``s``.
    """

    if len(term) > 3 and term.endswith("s") and not term.endswith("ss"):
        term = term[:-1]
    return term[:6]


def _token_counts(text: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for token in _TERM_PATTERN.findall(text.lower()):
        stem = _stem(token)
        counts[stem] = counts.get(stem, 0) + 1
    return counts


#: Section markers that introduce the part of a page holding a topic. A 10-K
#: page starts with the carry-over of the previous section, so the heading of
#: interest ("Item 3. Legal Proceedings.") is often in the middle of the page.
_SECTION_MARKER = re.compile(r"(?:item\s+\d+[a-z]?\.|note\s+\d+\s*[\u2014-])", re.IGNORECASE)


def _heading_text(text: str) -> str:
    """Return the page's own heading plus every section marker on the page."""

    parts = [text[:_HEADING_CHARS]]
    for match in _SECTION_MARKER.finditer(text):
        parts.append(text[match.start() : match.start() + 90])
    return "\n".join(parts)


def _require_text(value: object, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")


__all__ = [
    "PDFDocumentInput",
    "PDFPageInput",
    "PDFPageSource",
    "PDF_PAGE_SOURCE_DEFINITION",
    "PDF_PAGE_SOURCE_NAME",
    "parse_pdf_document",
    "pdf_page_source_id",
    "search_pdf_pages",
]
