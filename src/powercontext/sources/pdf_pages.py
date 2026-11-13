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

"""Offline PDF capture and scoped retrieval of exact physical page evidence."""

from __future__ import annotations

import io
import json
import math
import re
import unicodedata
from collections import Counter
from hashlib import sha256
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from powercontext.artifacts.models import MemoryCitation
from powercontext.sources.definitions import AdapterSourceDefinition, SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

if TYPE_CHECKING:
    from powercontext.builtin.runtime.application import ScopedMemoryApplication
    from powercontext.builtin.runtime.models import MemoryEntryRecord

__all__ = ["PDF_PAGE_SOURCE_DEFINITION", "PDFDocumentInput", "PDFPageInput", "parse_pdf_document", "search_pdf_pages"]

_SOURCE_TYPE = "pdf-page"


class PDFDocumentInput(BaseModel):
    """The complete bytes of one revision of a PDF document."""

    model_config = ConfigDict(frozen=True)

    document_id: str
    content: bytes


class PDFPageInput(BaseModel):
    """One physical page of an identified PDF revision."""

    model_config = ConfigDict(frozen=True)

    document_id: str
    document_sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    page_number: StrictInt = Field(ge=1)
    text: str


def parse_pdf_document(value: PDFDocumentInput) -> tuple[PDFPageInput, ...]:
    """Extract every page in order or fail before returning any pages."""

    from pypdf import PdfReader

    if not value.document_id.strip() or not value.content:
        raise ValueError("document_id and PDF bytes must be nonempty")  # noqa: TRY003
    digest = sha256(value.content).hexdigest()
    try:
        # PdfReader tries the empty password for encrypted files when no password is supplied.
        with PdfReader(io.BytesIO(value.content), strict=True) as reader:
            return tuple(
                PDFPageInput(
                    document_id=value.document_id,
                    document_sha256=digest,
                    page_number=number,
                    text=page.extract_text(),
                )
                for number, page in enumerate(reader.pages, start=1)
            )
    except Exception as error:
        # The PDF parser can raise built-in exceptions as well as PdfReadError on malformed objects.
        raise ValueError("could not read the complete PDF document") from error  # noqa: TRY003


class _PDFPageSource(Source):
    page: PDFPageInput


class _PDFPageAdapter:
    name = _SOURCE_TYPE
    input_class = PDFPageInput
    source_class = _PDFPageSource

    async def resolve(self, value: PDFPageInput, /) -> _PDFPageSource:
        if not value.document_id.strip():
            raise ValueError("document_id must be nonempty")  # noqa: TRY003
        # Encode the tuple unambiguously before hashing to support arbitrary document ID lengths.
        identity = json.dumps((value.document_id, value.document_sha256, value.page_number), separators=(",", ":"))
        return _PDFPageSource(
            name=f"pdf:{sha256(identity.encode()).hexdigest()}",
            materialization=SourceMaterialization.CAPTURED,
            page=value,
        )

    async def read(self, source: _PDFPageSource, /) -> PDFPageInput:
        return source.page


class _PDFPageTextProjection:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = _PDFPageSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: _PDFPageSource, /) -> TextEvidence:
        page = source.page
        return TextEvidence(
            source_type=_SOURCE_TYPE,
            source_id=source.name,
            content=f"{page.document_id}\n{page.text}",
            metadata=page.model_dump(exclude={"text"}),
        )


PDF_PAGE_SOURCE_DEFINITION: SourceDefinition[PDFPageInput, _PDFPageSource, PDFPageInput] = AdapterSourceDefinition(
    _PDFPageAdapter(), version="1", projections=(_PDFPageTextProjection(),)
)

_WORD = re.compile(r"[^\W\d_]+|\d+")
_SECTION_NUMBER = re.compile(r"^item \d+[a-z]?\s*[.:]\s*", re.IGNORECASE)
_CONTENTS_LINK = re.compile(r"(?:\.\s*){3,}\d+\s*$")
_TABLE_VALUE = re.compile(r"\s(?:[$€£]\s*)?\(?-?\d[\d,.]*(?:%|\))?(?=\s|$)")
_FILING_YEAR = re.compile(r"[_\W]+(?:19|20)\d{2}(?:Q[1-4])?(?:[_\W]|$)", re.IGNORECASE)
_IDENTITY_WORD = re.compile(r"[^\W_]+")
_STATEMENT_NUMBER = re.compile(r"\d{1,3}(?:,\d{3})+|\d+\.\d+|\$\s*\d+")
_REGION = (
    "north america",
    "latin america",
    "south america",
    "europe",
    "middle east",
    "africa",
    "asia",
    "pacific",
    "oceania",
)
_YEAR = re.compile(r"(?:19|20)\d{2}\Z")
_BOILERPLATE = frozenset(
    "a an and are as at be been by can calculate could did do does for from had has have how i if in into is it "  # noqa: SIM905
    "its me of on or should that the their them then these they this to was were what when where which who will "
    "with would you your please answer assume assuming based details directly explicitly fy million millions only "
    "outlined perspective provide reported respond shown stated use using usd".split()
)

# Financial terminology varies across reports and between report headings and prose.
_CONCEPTS = (
    (
        "income statement",
        "statement of income",
        "statements of operations",
        "profit and loss",
        "earnings statement",
        "p&l statement",
    ),
    ("balance sheet", "statement of financial position"),
    ("cash flow", "statement of cash flows", "cash provided by operating activities"),
    ("capital expenditures", "capex", "capital spending", "purchases of property plant and equipment"),
    ("revenue", "net sales", "net revenues", "total sales"),
    ("operating income", "operating profit", "income from operations", "earnings from operations"),
    ("depreciation and amortization", "depreciation amortization", "depreciation", "d&a"),
    ("litigation", "legal proceedings", "legal matters", "legal battles", "contingencies"),
    (
        "geographies",
        "geographic",
        "geographical",
        "international operations",
        "reportable segments",
        "operating segments",
        "geographic regions",
        "our operations",
    ),
    ("restructuring costs", "restructuring charges", "restructuring expenses"),
    ("total debt", "borrowings", "debt obligations"),
    ("share repurchase", "stock repurchase", "share buyback"),
    ("cost of revenue", "cost of sales", "cost of goods sold"),
    ("research and development", "research development"),
    ("selling general and administrative", "selling and administrative", "sga"),
)

_STATEMENT_TITLES = {
    "income": (
        "statement of income",
        "statement of operations",
        "statement of earnings",
        "income statement",
        "profit and loss",
        "earnings statement",
        "statement of profit",
        "statement of comprehensive income",
    ),
    "balance": ("balance sheet", "statement of financial position", "statement of assets and liabilities"),
    "cash": ("statement of cash flows", "cash flow statement"),
    "operations": (
        "results of operations",
        "analysis of consolidated sales",
        "management discussion and analysis",
        "financial review",
        "operating results",
    ),
}

# Some questions name a financial measure without naming its underlying statement.
_MEASURE_STATEMENTS = (
    (("gross margin", "gross profit", "cost of goods sold", "cogs"), ("income", "operations")),
    (
        ("operating margin", "operating profit", "operating income", "ebitda", "net income", "earnings per share"),
        ("income", "operations"),
    ),
    (("revenue", "sales growth", "high growth", "growth rate", "financials"), ("income", "operations")),
    (("inventory turnover", "days payable outstanding", "return on assets"), ("balance", "income")),
    (
        (
            "cash flow",
            "cashflow",
            "capital expenditures",
            "capex",
            "cash generation",
            "free cash flow",
            "cash conversion",
        ),
        ("cash",),
    ),
    (
        ("property plant and equipment", "capital intensive", "capital intensity", "ppne", "ppe"),
        ("balance", "cash", "income"),
    ),
    (("quick ratio", "current ratio", "working capital", "liquidity", "total assets", "debt"), ("balance",)),
)


def _words(text: str) -> list[str]:
    text = re.sub(r"(?<=\w)-[ \t]*\n[ \t]*(?=\w)", "", unicodedata.normalize("NFKC", text).casefold())
    return _WORD.findall(text)


def _term(word: str) -> str:
    if len(word) > 4 and word.endswith(("ies", "sses", "xes")):
        return word[:-3] + "y" if word.endswith("ies") else word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def _phrase(words: list[str]) -> str:
    return " " + " ".join(_term(word) for word in words) + " "


def _query_terms(question: str) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
    words = _words(question)
    phrase = _phrase(words)
    concepts = tuple(group for group in _CONCEPTS if any(_phrase(_words(alias)) in phrase for alias in group))
    important = tuple(dict.fromkeys(word for word in words if word not in _BOILERPLATE and len(word) > 1))
    return important, concepts


def _filing_matches(label: str, query_words: tuple[str, ...]) -> int:
    name = _FILING_YEAR.split(label, maxsplit=1)[0]
    company_words = _IDENTITY_WORD.findall(name.casefold())
    if not company_words:
        return 0
    company = "".join(company_words)
    for start in range(len(query_words)):
        candidate = ""
        for word in query_words[start:]:
            candidate += word
            if candidate == company:
                return 2
            if not company.startswith(candidate):
                break
    if len(company_words) > 1:
        initials = tuple(word[0] for word in company_words)
        # Recognize initialisms and names joined by a spoken "and".
        if "".join(initials) in query_words or "n".join(initials) in query_words:
            return 1
    return 0


def _statement_types(question: str) -> tuple[str, ...]:
    query = _phrase(_words(question))
    explicit = [
        key for key, titles in _STATEMENT_TITLES.items() if any(_phrase(_words(title)) in query for title in titles)
    ]
    inferred = [
        key
        for measures, keys in _MEASURE_STATEMENTS
        if any(_phrase(_words(measure)) in query for measure in measures)
        for key in keys
    ]
    return tuple(dict.fromkeys((*explicit, *inferred)))


def _statement_score(
    key: str, title_position: int | None, consolidated: bool, number_count: int, beginning: str
) -> float:
    score = 3.0 if title_position is None else 12.0 - 0.7 * title_position
    if consolidated:
        score += 3.0
    if key == "operations":
        if " consolidated sale " in beginning or " result of operation " in beginning:
            score += 2.0
    else:
        score += min(number_count, 20) * 0.3
        if number_count < 8:
            score -= 8.0
    return score


def _prioritize_statements(
    ranked: list[MemoryEntryRecord], statement_types: tuple[str, ...]
) -> list[MemoryEntryRecord]:
    if not statement_types:
        return ranked
    titles = {key: tuple(_phrase(_words(title)) for title in names) for key, names in _STATEMENT_TITLES.items()}
    candidates: dict[str, list[tuple[float, int, MemoryEntryRecord]]] = {key: [] for key in statement_types}
    for rank, record in enumerate(ranked):
        body = record.entry.text.partition("\n")[2]
        beginning = _phrase(_words(body)[:65])
        matching_types = {key for key, aliases in titles.items() if any(alias in beginning for alias in aliases)}
        if matching_types.isdisjoint(statement_types):
            continue
        first_lines = [line for line in body.splitlines() if line.strip()][:10]
        lines = [_phrase(_words(line)) for line in first_lines]
        number_count = len(_STATEMENT_NUMBER.findall(body))
        consolidated = any(" consolidated " in line for line in lines[:5])
        for key in statement_types:
            if key not in matching_types:
                continue
            title_position = next(
                (index for index, line in enumerate(lines) if any(alias in line for alias in titles[key])), None
            )
            score = _statement_score(key, title_position, consolidated, number_count, beginning)
            # A page that names several statements early is often a contents page.
            score -= 6.0 * sum(titles[other][0] in beginning for other in matching_types - {key})
            score += 10.0 / (10 + rank)
            candidates[key].append((score, rank, record))
    prioritized = {record.entry.entry_id: record for record in ranked[:3]}
    for pool in candidates.values():
        pool.sort(key=lambda item: (-item[0], item[1]))
        for _, _, record in pool[:3]:
            prioritized.setdefault(record.entry.entry_id, record)
    for record in ranked:
        prioritized.setdefault(record.entry.entry_id, record)
    return list(prioritized.values())


def _page_labels(body: str) -> tuple[list[str], list[str], int]:
    headings: list[str] = []
    rows: list[str] = []
    contents_links = 0
    for raw_line in body.splitlines():
        line = _SECTION_NUMBER.sub("", raw_line.strip())
        if _CONTENTS_LINK.search(line):
            contents_links += 1
        elif number := _TABLE_VALUE.search(line):
            rows.append(_phrase(_words(line[: number.start()])))
        elif 1 < len(line.split()) <= 7:
            headings.append(_phrase(_words(line)))
    return headings, rows, contents_links


def _cover_concepts(
    scored: list[tuple[MemoryEntryRecord, float, tuple[float, ...]]], concept_count: int
) -> list[MemoryEntryRecord]:
    # Calculations can require evidence from different statements. Reserve one
    # high-confidence page per concept before filling from the combined ranking.
    selected = {record.entry.entry_id: record for record, _, _ in scored[:1]}
    for index in range(concept_count):
        eligible = (item for item in scored if item[2][index] >= 7.0)
        best = max(eligible, key=lambda item: item[1] - sum(item[2]) + 2 * item[2][index], default=None)
        if best is not None:
            selected.setdefault(best[0].entry.entry_id, best[0])
    for record, _, _ in scored:
        selected.setdefault(record.entry.entry_id, record)
    return list(selected.values())


def _rank_pages(
    records: list[MemoryEntryRecord], important: tuple[str, ...], concepts: tuple[tuple[str, ...], ...]
) -> list[MemoryEntryRecord]:
    wanted = set(map(_term, important))
    phrases = tuple(tuple(_phrase(_words(alias)) for alias in group) for group in concepts)
    pages = []
    frequencies: Counter[str] = Counter()
    for record in records:
        label, _, body = record.entry.text.partition("\n")
        words = _words(body)
        counts = Counter(map(_term, words))
        frequencies.update(wanted.intersection(counts))
        pages.append((record, label, body, words, counts))
    if not pages:
        return []
    average_length = max(1, sum(len(words) for _, _, _, words, _ in pages) / len(pages))
    weights = {term: math.log1p((len(pages) - frequencies[term] + 0.5) / (frequencies[term] + 0.5)) for term in wanted}

    geographic_query = any("geographies" in group for group in concepts)

    def score(
        page: tuple[MemoryEntryRecord, str, str, list[str], Counter[str]],
    ) -> tuple[float, tuple[float, ...]]:
        _, label, body, words, counts = page
        identity_terms = set(map(_term, _words(label)))
        matches = wanted.intersection(identity_terms)
        body_terms = wanted - matches
        body_score = 0.0
        length_norm = 1.2 * (0.65 + 0.35 * len(words) / average_length)
        for term in sorted(body_terms.intersection(counts)):
            weight = weights[term] * (0.2 if _YEAR.fullmatch(term) else 1.0)
            body_score += weight * (2.2 * counts[term] / (counts[term] + length_norm))
        normalized = _phrase(words)
        headings, rows, contents_links = _page_labels(body)

        def concept_match(group: tuple[str, ...]) -> float:
            if any(alias in heading for alias in group for heading in headings):
                return 9.0
            if any(alias in row for alias in group for row in rows):
                return 7.0
            return 1.2 if any(alias in normalized for alias in group) else 0.0

        concept_scores = tuple(concept_match(group) for group in phrases)
        concept_score = sum(concept_scores)
        if geographic_query:
            concept_score += 2.0 * sum(_phrase(_words(region)) in normalized for region in _REGION)
        identity_score = sum(0.6 if _YEAR.fullmatch(term) else 4.0 for term in matches)
        contents_penalty = 8.0 if contents_links >= 4 else 0.0
        return body_score + concept_score + identity_score - contents_penalty, concept_scores

    scored = [(page[0], *score(page)) for page in pages]
    scored.sort(key=lambda item: (-item[1], item[0].entry.entry_id))
    if len(phrases) < 2:
        return [record for record, _, _ in scored]
    return _cover_concepts(scored, len(phrases))


async def search_pdf_pages(
    memory: ScopedMemoryApplication, question: str, limit: int = 10
) -> tuple[MemoryCitation, ...]:
    """Search scoped Memory offline and return exact citations to distinct PDF pages."""

    if type(limit) is not int or limit < 0:
        raise ValueError("limit must be a nonnegative integer")  # noqa: TRY003
    if limit == 0 or not question.strip():
        return ()

    important, concepts = _query_terms(question)
    if not important and not concepts:
        return ()
    # Use the current scoped Memory snapshot, so pages can compete even when their
    # wording would prevent them from reaching a keyword shortlist.
    records = [
        record
        for record in (await memory.list()).entries
        if len(record.entry.sources) == 1 and record.entry.sources[0].source_type == _SOURCE_TYPE
    ]

    query_words = tuple(_IDENTITY_WORD.findall(question.casefold()))
    labels = {record.entry.entry_id: record.entry.text.partition("\n")[0] for record in records}
    matches = {label: _filing_matches(label, query_words) for label in set(labels.values())}
    best_match = max(matches.values(), default=0)
    primary = [record for record in records if not best_match or matches[labels[record.entry.entry_id]] == best_match]
    ranked = _prioritize_statements(_rank_pages(primary, important, concepts), _statement_types(question))
    if best_match and len({record.entry.sources[0].source_id for record in primary}) < limit:
        remaining = [record for record in records if matches[labels[record.entry.entry_id]] != best_match]
        ranked.extend(_rank_pages(remaining, important, concepts))

    citations: list[MemoryCitation] = []
    seen_pages: set[str] = set()
    for record in ranked:
        page_id = record.entry.sources[0].source_id
        if page_id not in seen_pages:
            seen_pages.add(page_id)
            citations.append(record.citation)
            if len(citations) == limit:
                break
    return tuple(citations)
