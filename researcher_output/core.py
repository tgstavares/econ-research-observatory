from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict
import datetime as dt
from functools import lru_cache
from html import unescape
from html.parser import HTMLParser
import hashlib
import json
from math import fsum
from pathlib import Path
import re
import sqlite3
import unicodedata
from urllib.parse import unquote, urlparse
import uuid

NAMESPACE = uuid.UUID("cdd89763-7e2b-49d5-9231-3669dafb6465")
ARTICLE_TYPES = {"journal-article", "article", "review"}
REPEC_SCORE_METRIC = "repec.recurse"
REPEC_SCORE_HORIZON = "all"
EDITORIAL_REVIEWS = Path(__file__).resolve().parents[1] / 'data/publication_reviews/editorial_dois.json'


def repec_summary_policy():
    return dict(metric_id=REPEC_SCORE_METRIC, horizon=REPEC_SCORE_HORIZON,
                counting=["full", "fractional"], missing_values="missing_with_coverage")


def uses_current_repec_policy(bundle):
    policy = bundle.get("policy", {}).get("repec_summary", {})
    return policy.get("metric_id") == REPEC_SCORE_METRIC and policy.get("horizon") == REPEC_SCORE_HORIZON


def text(value):
    value = unescape(unescape(str(value or "")))
    return " ".join(re.sub(r"<[^>]*>", " ", value).split())


def key(value):
    return "".join(c for c in unicodedata.normalize("NFKC", text(value)).casefold().replace("&", " and ") if c.isalnum())


def normalize_orcid(value):
    value = re.sub(r"^https?://(?:www\.)?orcid\.org/", "", str(value).strip(), flags=re.I).rstrip("/").upper()
    digits = value.replace("-", "")
    if not re.fullmatch(r"\d{15}[\dX]", digits):
        raise ValueError("An ORCID must contain 16 characters, including its final check digit.")
    total = 0
    for digit in digits[:15]:
        total = (total + int(digit)) * 2
    check = (12 - total % 11) % 11
    if digits[-1] != ("X" if check == 10 else str(check)):
        raise ValueError("ORCID check digit is invalid.")
    return "-".join(digits[i:i + 4] for i in range(0, 16, 4))


def same_orcid(value, expected):
    try:
        return normalize_orcid(value) == expected
    except ValueError:
        return False


def normalize_doi(value):
    value = unquote(str(value or "").strip())
    value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", value, flags=re.I).casefold()
    return value if re.fullmatch(r"10\.\d{4,9}/\S+", value) else None


def normalize_issn(value):
    compact = "".join(c for c in unicodedata.normalize("NFKC", str(value or ""))
                      if not c.isspace() and unicodedata.category(c) not in {"Pd", "Cf"}).upper()
    if not re.fullmatch(r"\d{7}[\dX]", compact):
        return None
    digits = [int(c) for c in compact[:7]] + [10 if compact[-1] == "X" else int(compact[-1])]
    if sum(d * w for d, w in zip(digits, range(8, 0, -1))) % 11:
        return None
    return compact[:4] + "-" + compact[4:]


def crossref_date(value):
    dates = (value or {}).get("date-parts") or []
    parts = dates[0] if dates and isinstance(dates[0], list) else []
    known = []
    for i, part in enumerate(parts[:3]):
        if part is None or isinstance(part, bool):
            break
        try:
            number = int(part)
            dt.date(*(known + [number] + [1] * (2 - i)))
        except (TypeError, ValueError, OverflowError):
            break
        known.append(number)
    return "-".join(str(v).zfill(4 if i == 0 else 2) for i, v in enumerate(known)) or None


@dataclass
class Record:
    provider: str
    source_key: str
    raw_file: str
    title: str
    doi: str | None = None
    journal: str = ""
    issns: list[str] = field(default_factory=list)
    kind: str = ""
    bibliographic_date: str | None = None
    online_date: str | None = None
    author_count: int | None = None
    authors: list[dict] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def parse_orcid(body, raw_file):
    records = []
    for group in body.get("group", []):
        for work in group.get("work-summary", []):
            ids = (work.get("external-ids") or {}).get("external-id", [])
            dois = sorted({v for item in ids if item.get("external-id-type", "").lower() == "doi"
                           and item.get("external-id-relationship") == "self"
                           if (v := normalize_doi(item.get("external-id-value")))})
            date = work.get("publication-date") or {}
            parts = [(date.get(k) or {}).get("value") for k in ("year", "month", "day")]
            date_string = "-".join(str(v).zfill(4 if i == 0 else 2) for i, v in enumerate(parts) if v) or None
            records.append(Record("orcid", str(work["put-code"]), raw_file,
                text((work.get("title") or {}).get("title", {}).get("value")),
                doi=dois[0] if len(dois) == 1 else None,
                journal=text((work.get("journal-title") or {}).get("value")),
                issns=[str(i.get("external-id-value", "")) for i in ids if i.get("external-id-type", "").lower() == "issn"],
                kind=work.get("type", ""), bibliographic_date=date_string,
                evidence=["orcid_record"], notes=["multiple_self_dois"] if len(dois) > 1 else []))
    return records


def parse_crossref(work, raw_file, orcid):
    authors = [{"name": text(a.get("name") or " ".join(filter(None, [a.get("given"), a.get("family")]))),
                "orcid": a.get("ORCID"), "role": "author"} for a in work.get("author", [])]
    title = (work.get("title") or [""])[0]
    journal = (work.get("container-title") or [""])[0]
    return Record("crossref", str(work.get("DOI", "")), raw_file, text(title),
        doi=normalize_doi(work.get("DOI")), journal=text(journal), issns=work.get("ISSN", []),
        kind=work.get("type", ""),
        bibliographic_date=crossref_date(work.get("published-print")) or crossref_date(work.get("issued")) or crossref_date(work.get("published")),
        online_date=crossref_date(work.get("published-online")), author_count=len(authors) or None,
        authors=authors, evidence=["crossref_orcid"] if any(same_orcid(a.get("orcid"), orcid) for a in authors) else [])


def parse_openalex(work, raw_file, orcid, author_id):
    location = work.get("primary_location") or {}
    source = location.get("source") or {}
    authorships = work.get("authorships") or []
    # author.orcid describes the disambiguated profile; only raw_orcid is an article-level assertion.
    direct = any(same_orcid(a.get("raw_orcid"), orcid) for a in authorships)
    in_profile = any((a.get("author") or {}).get("id") == author_id for a in authorships)
    authors = [{"name": text(a.get("raw_author_name") or (a.get("author") or {}).get("display_name")),
                "orcid": a.get("raw_orcid"), "openalex_id": (a.get("author") or {}).get("id"),
                "role": "author"} for a in authorships]
    return Record("openalex", work["id"], raw_file, text(work.get("title")),
        doi=normalize_doi(work.get("doi")), journal=text(source.get("display_name")),
        issns=list(source.get("issn") or []), kind=work.get("type", ""),
        bibliographic_date=str(work.get("publication_year") or "") or None,
        online_date=work.get("publication_date") if source.get("type") == "journal" and work.get("type") in ARTICLE_TYPES else None,
        author_count=len(authors) if 0 < len(authors) < 100 else None,
        authors=authors, evidence=["openalex_raw_orcid"] if direct else (["openalex_profile"] if in_profile else []),
        notes=["openalex_authorships_may_be_truncated"] if len(authors) >= 100 else [])


def deduplicate(records):
    """DOIs are authoritative; metadata matches only attach DOI-less records to unique candidates."""
    groups = defaultdict(list)
    fingerprints = defaultdict(set)
    def fingerprint(r):
        if r.title and r.journal and r.bibliographic_date:
            return (key(r.title), key(r.journal), r.bibliographic_date[:4])
        return None
    for r in records:
        if r.doi:
            groups["doi:" + r.doi].append(r)
            if fp := fingerprint(r):
                fingerprints[fp].add("doi:" + r.doi)
    for r in records:
        if r.doi:
            continue
        fp = fingerprint(r)
        candidates = fingerprints.get(fp, set()) if fp else set()
        if len(candidates) == 1:
            group_key = next(iter(candidates))
        elif fp:
            group_key = "metadata:" + hashlib.sha256(json.dumps(fp).encode()).hexdigest()
        else:
            group_key = f"source:{r.provider}:{r.source_key}"
        groups[group_key].append(r)
    publications = []
    priority = {"crossref": 0, "orcid": 1, "openalex": 2, "reviewed_institutional": 3}
    for identity, group in sorted(groups.items()):
        group = sorted(group, key=lambda r: (priority[r.provider], r.source_key))
        def first(attr):
            return next((getattr(r, attr) for r in group if getattr(r, attr)), None)
        online = sorted(r.online_date for r in group if r.online_date)
        counts = {r.author_count for r in group if r.author_count is not None}
        notes = sorted({note for r in group for note in r.notes})
        if len(counts) > 1:
            notes.append("conflicting_author_counts")
        if len({r.bibliographic_date[:4] for r in group if r.bibliographic_date}) > 1:
            notes.append("different_bibliographic_years")
        publications.append(dict(publication_id=str(uuid.uuid5(NAMESPACE, identity)),
            identity=identity, doi=first("doi"), title=first("title") or "Untitled record",
            journal_title=first("journal") or "", kind=first("kind") or "unknown",
            bibliographic_date=first("bibliographic_date"), online_date=online[0] if online else None,
            author_count=first("author_count") if len(counts) <= 1 else None,
            authors=first("authors") or [], issns=sorted({i for r in group for i in r.issns}),
            author_evidence=sorted({e for r in group for e in r.evidence}), notes=notes,
            sources=[asdict(r) for r in group]))
    return publications


class RankingCatalog:
    def __init__(self, path):
        self.path = path
        self.connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        self.connection.row_factory = sqlite3.Row
        self.issns = dict(self.connection.execute("SELECT issn,journal_id FROM unique_journal_issns"))
        self.rows = defaultdict(list)
        for r in self.connection.execute("SELECT * FROM current_observations"):
            self.rows[r["journal_id"]].append(dict(r))
        self.aliases = defaultdict(set)
        for jid, title in self.connection.execute("SELECT DISTINCT journal_id,title FROM source_records WHERE journal_id IS NOT NULL"):
            self.aliases[key(title)].add(jid)
        from .journal_reviews import apply_reviewed_journals
        self.journal_review_policy = apply_reviewed_journals(self)

    def match(self, publication):
        # Some ORCID sources put print/electronic ISSNs in one field. Retain
        # the original field in the source record, but validate each token.
        normalized = {v for raw in publication["issns"]
                      for token in [raw, *re.split(r"[;,\s]+", str(raw))]
                      if (v := normalize_issn(token))}
        matches = {self.issns[v] for v in normalized if v in self.issns}
        candidates = sorted(self.aliases[key(publication["journal_title"])]) if publication["journal_title"] else []
        if len(matches) == 1:
            return next(iter(matches)), "issn", candidates
        return None, "conflicting_issns" if matches else "unmatched", candidates

    def scope(self, journal_id):
        rows = self.rows.get(journal_id, [])
        evidence = []
        for r in rows:
            source, field_code = r["source_id"], str(r.get("field_code") or "").upper()
            positive = source == "jcr" and r["category"] in {"ECONOMICS", "BUSINESS, FINANCE"}
            positive |= source == "abs" and field_code in {"ECON", "FINANCE"}
            codes = re.findall(r"\d+", field_code)
            positive |= source == "abdc" and any(c.startswith("38") or c == "3502" for c in codes)
            if positive:
                evidence.append(f"{source} {r['reference_year']}: {field_code or r['category']}")
        reviewed = getattr(self, "reviewed_scopes", {}).get(journal_id, [])
        evidence.extend(reviewed)
        return ("econ_finance" if evidence else ("other_classified_journal" if rows else "unknown")), sorted(set(evidence))

    def repec_candidates(self, journal_id):
        return [dict(r) for r in self.connection.execute("""SELECT DISTINCT m.source_journal_id, i.value AS handle,
                j.preferred_title FROM journal_match_candidates m
                JOIN journal_identifiers i ON i.journal_id=m.source_journal_id AND i.scheme='repec_handle'
                JOIN journals j ON j.journal_id=m.source_journal_id WHERE m.candidate_journal_id=?""", (journal_id,))]

    def repec_url(self, handle):
        row = self.connection.execute("SELECT raw_metadata_json FROM source_records WHERE source_key=? LIMIT 1", (handle,)).fetchone()
        return json.loads(row[0])["primary_url"] if row else None


class SeriesISSNs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.pending = False
        self.issns = set()

    def handle_data(self, data):
        label = data.strip().rstrip(":").upper()
        if label in {"ISSN", "E-ISSN", "EISSN"}:
            self.pending = True
        elif self.pending and data.strip():
            for raw in re.findall(r"\b\d{4}[-–]\d{3}[\dXx]\b", data):
                if value := normalize_issn(raw):
                    self.issns.add(value)
            self.pending = False


def validate_overrides(publications, overrides):
    if not isinstance(overrides, dict):
        raise ValueError("Manual overrides must be a JSON object.")
    lookup = {p["publication_id"]: p["publication_id"] for p in publications}
    lookup.update({p["doi"]: p["publication_id"] for p in publications if p["doi"]})
    normalized = {}
    for identifier, override in overrides.items():
        identifier = normalize_doi(identifier) or identifier
        if identifier not in lookup:
            raise ValueError(f"Manual override does not match a retrieved publication: {identifier}")
        if not isinstance(override, dict) or set(override) - {"include", "author_count", "note"}:
            raise ValueError("Override fields are include, author_count, and note.")
        if not isinstance(override.get("note"), str) or not override["note"].strip():
            raise ValueError("Each manual override requires an explanatory note.")
        if "include" in override and not isinstance(override["include"], bool):
            raise ValueError("Manual include must be true or false, not a string.")
        if "author_count" in override and (not isinstance(override["author_count"], int) or isinstance(override["author_count"], bool) or override["author_count"] < 1):
            raise ValueError("Manual author_count must be a positive integer.")
        publication_id = lookup[identifier]
        if publication_id in normalized:
            raise ValueError("Multiple overrides address the same publication.")
        normalized[publication_id] = override
    return normalized


@lru_cache(maxsize=1)
def _reviewed_editorial_dois(path, modified_ns):
    document = json.loads(path.read_text())
    if document.get('version') != 1:
        raise ValueError('Unsupported editorial review format.')
    reviewed = set()
    for item in document['documents']:
        doi, url = normalize_doi(item['doi']), urlparse(item['source_url'])
        if (not doi or doi in reviewed or item['document_type'] != 'editorial'
                or url.scheme not in {'http', 'https'} or not url.netloc or not item['note'].strip()):
            raise ValueError('An editorial review requires a unique DOI, publisher source and explanation.')
        dt.date.fromisoformat(item['reviewed_at'])
        reviewed.add(doi)
    return frozenset(reviewed)


def is_editorial_material(publication):
    """Identify editorial subtypes even when a provider uses journal-article."""
    title = publication['title']
    reviewed = _reviewed_editorial_dois(EDITORIAL_REVIEWS, EDITORIAL_REVIEWS.stat().st_mtime_ns) if EDITORIAL_REVIEWS.exists() else ()
    return bool(normalize_doi(publication.get('doi')) in reviewed or publication.get('kind') == 'editorial'
        or re.match(r"^(?:(?:guest\s+)?editorial|preface|foreword)\b", title, re.I)
        or re.search(r"\b(?:guest\s+)?editors?(?:['’ʼ`]\s*|\s+)introduction\b", title, re.I)
        or (re.search(r"\bspecial\s+issue\b", title, re.I) and re.search(r"\bintroduction\b", title, re.I))
        or any(source.get('kind') == 'editorial' for source in publication.get('sources', [])))


def document_type_needs_review(publication):
    return bool(re.match(r"^(?:erratum|corrigendum|correction|book review)\b", publication['title'], re.I))


def assess(publications, catalog, repec_links, *, as_of, from_year=None, to_year=None, year_policy="bibliographic", accept_profile=False, overrides=None):
    overrides = validate_overrides(publications, overrides or {})
    for p in publications:
        p["journal_id"], p["journal_match"], p["journal_candidates"] = catalog.match(p)
        p["scope"], p["scope_evidence"] = catalog.scope(p["journal_id"])
        date = (p["online_date"] or p["bibliographic_date"]) if year_policy == "earliest" else (p["bibliographic_date"] or p["online_date"])
        p["publication_year"] = int(date[:4]) if date and re.match(r"\d{4}", date) else None
        strong = any(e != "openalex_profile" for e in p["author_evidence"])
        p["author_match"] = "asserted_orcid" if strong else "profile_candidate"
        if strong and set(p["author_evidence"]) <= {"reviewed_institutional_authorship", "openalex_profile"}:
            p["author_match"] = "reviewed_authorship"
        p["state"], p["reason"] = "included", "journal_article_in_econ_finance"
        if is_editorial_material(p):
            p["state"], p["reason"] = "excluded", "editorial_material"
        elif p["kind"] not in ARTICLE_TYPES:
            p["state"], p["reason"] = "excluded", "non_journal_publication_type"
        elif document_type_needs_review(p):
            p["state"], p["reason"] = "review", "check_document_type"
        elif not strong and not accept_profile:
            p["state"], p["reason"] = "review", "authorship_needs_confirmation"
        elif p["journal_id"] is None:
            p["state"], p["reason"] = "review", "journal_match_needs_confirmation"
        elif p["scope"] == "unknown":
            p["state"], p["reason"] = "review", "journal_field_scope_unknown"
        elif p["scope"] != "econ_finance":
            p["state"], p["reason"] = "excluded", "outside_journal_field_scope"
        elif p["publication_year"] is None:
            p["state"], p["reason"] = "review", "publication_year_missing"
        elif date > as_of:
            p["state"], p["reason"] = "excluded", "publication_date_after_snapshot"
        elif (from_year and p["publication_year"] < from_year) or (to_year and p["publication_year"] > to_year):
            p["state"], p["reason"] = "excluded", "outside_year_window"
        override = overrides.get(p["publication_id"], {})
        if override:
            if "author_count" in override:
                p["author_count"] = override["author_count"]
                p["author_count_status"] = "reviewed_override"
            if "include" in override:
                p["state"] = "included" if override["include"] else "excluded"
                p["reason"] = "manual_override"
            p["notes"].append("Manual review: " + override["note"])
        p["fractional_credit"] = 1 / p["author_count"] if p["author_count"] else None
        apply_rankings(p, catalog, repec_links)
    return publications


def apply_rankings(p, catalog, repec_links):
    """Recalculate journal metrics without changing a publication decision."""
    rankings = list(catalog.rows.get(p["journal_id"], []))
    resolutions = [link for link in repec_links.get(p["journal_id"], [])
                   if link.get("method") == "reviewed_snapshot_unavailable"]
    verified = [link for link in repec_links.get(p["journal_id"], []) if link.get("verified")]
    selected = [link for link in verified if link.get("score_selected")]
    if len(selected) == 1:
        # Retain every identity link as evidence, but use only the explicitly
        # reviewed publisher archive for this frozen all-period snapshot.
        verified = selected
        rankings = [r for r in rankings if r["source_id"] != "repec"]
        rankings.extend(r for r in catalog.rows[selected[0]["source_journal_id"]] if r["source_id"] == "repec")
        p["repec_note"] = selected[0]["score_selection"]["note"]
    if len(verified) == 1:
        p["repec_status"] = {"series_metadata_issn": "verified_series_issn",
            "article_identifier_to_crossref_issn": "verified_article_identifier",
            "reviewed_article_citation_to_crossref_issn": "verified_reviewed_citation",
            "reviewed_publisher_journal_issn": "verified_publisher_identity"}.get(
                verified[0].get("method", "series_metadata_issn"), "verified_reviewed_link")
        if not selected and verified[0]["source_journal_id"] != p["journal_id"]:
            rankings.extend(catalog.rows[verified[0]["source_journal_id"]])
    else:
        p["repec_status"] = "multiple_series_review" if verified else "not_verified"
        if not verified and resolutions:
            p["repec_status"] = "unavailable_in_snapshot"
            p["repec_note"] = resolutions[0]["note"]
    if p["repec_status"] != "unavailable_in_snapshot" and not selected:
        p.pop("repec_note", None)
    p["rankings"] = rankings
    for metric, col in (("abs.rating", "abs_grade"), ("abdc.rating", "abdc_grade"), ("jcr.ais", "ais")):
        values = {r["grade_value"] if col != "ais" else r["numeric_value"] for r in rankings if r["metric_id"] == metric and r["value_status"] == "reported"}
        p[col] = next(iter(values)) if len(values) == 1 else None
    aggregate = [r for r in rankings if r["metric_id"] == "repec.aggregate" and r["horizon"] == "all"]
    p["repec_rank_all"] = aggregate[0]["rank"] if len(aggregate) == 1 else None
    p["repec_score_all"] = aggregate[0]["numeric_value"] if len(aggregate) == 1 else None
    recursive = {r["observation_id"]: r for r in rankings
                 if r["source_id"] == "repec" and r["metric_id"] == REPEC_SCORE_METRIC
                 and r["horizon"] == REPEC_SCORE_HORIZON}
    row = next(iter(recursive.values())) if len(recursive) == 1 and len(verified) == 1 else {}
    p["repec_recursive_if_all"] = row.get("numeric_value") if row.get("value_status") == "reported" else None
    p["fractional_repec_recursive_if_all"] = (
        p["repec_recursive_if_all"] * p["fractional_credit"]
        if p["repec_recursive_if_all"] is not None and p["fractional_credit"] is not None else None)


def summarize(publications):
    included = [p for p in publications if p["state"] == "included"]
    with_fraction = [p for p in included if p["fractional_credit"] is not None]
    with_ais = [p for p in included if p["ais"] is not None]
    weighted = [p for p in with_ais if p["fractional_credit"] is not None]
    with_recursive = [p for p in included if p["repec_recursive_if_all"] is not None]
    fractional_recursive = [p for p in included if p["fractional_repec_recursive_if_all"] is not None]
    by_year = []
    for year in sorted({p["publication_year"] for p in included}, key=lambda y: (y is None, y or 0)):
        group = [p for p in included if p["publication_year"] == year]
        credits = [p["fractional_credit"] for p in group if p["fractional_credit"] is not None]
        by_year.append(dict(year=year, publications=len(group), fractional_sum=sum(credits) if credits else None,
                            fractional_coverage=len(credits)))
    return dict(discovered_distinct_records=len(publications), states=dict(Counter(p["state"] for p in publications)),
        publications=len(included), fractional_publications=sum(p["fractional_credit"] for p in with_fraction) if with_fraction else (None if included else 0),
        fractional_coverage=len(with_fraction), abs_distribution=dict(Counter(p["abs_grade"] or "unavailable" for p in included)),
        abdc_distribution=dict(Counter(p["abdc_grade"] or "unavailable" for p in included)),
        ais_sum=sum(p["ais"] for p in with_ais) if with_ais else (None if included else 0), ais_mean=(sum(p["ais"] for p in with_ais) / len(with_ais) if with_ais else None),
        ais_coverage=len(with_ais), fractional_ais_sum=sum(p["ais"] * p["fractional_credit"] for p in weighted) if weighted else (None if included else 0),
        fractional_ais_coverage=len(weighted), repec_coverage=sum(p["repec_rank_all"] is not None for p in included),
        repec_recursive_if_all_sum=fsum(p["repec_recursive_if_all"] for p in with_recursive) if with_recursive else (None if included else 0),
        repec_recursive_if_all_coverage=len(with_recursive),
        fractional_repec_recursive_if_all_sum=fsum(p["fractional_repec_recursive_if_all"] for p in fractional_recursive) if fractional_recursive else (None if included else 0),
        fractional_repec_recursive_if_all_coverage=len(fractional_recursive), by_year=by_year)
