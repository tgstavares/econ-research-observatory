"""Persistent researcher catalog and comparisons under current affiliation."""
from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import contextmanager
import datetime as dt
import fcntl
import hashlib
import json
from math import fsum
from pathlib import Path
import shutil
import sqlite3
import tempfile
import uuid

from .core import normalize_doi, normalize_orcid, key, REPEC_SCORE_METRIC, REPEC_SCORE_HORIZON, uses_current_repec_policy, is_editorial_material
from .coverage import publication_coverage, publication_source_policy

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE = ROOT / "data/researchers/researchers.sqlite"
DEFAULT_RANKINGS = ROOT / "data/processed/2026-10-14/journal_rankings.sqlite"
NAMESPACE = uuid.UUID("f7fce3b6-6cf3-4464-89b4-613b3aed1ed4")
ACTIVE_STATUSES = {"listed", "confirmed"}


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def stable_id(value):
    return str(uuid.uuid5(NAMESPACE, value))


def ranking_digest(path=DEFAULT_RANKINGS):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


class ResearcherCatalog:
    def __init__(self, path=DEFAULT_DATABASE):
        self.path = Path(path).resolve()
        if not self.path.exists():
            with self.transaction():
                pass

    @contextmanager
    def transaction(self):
        """Stage each write outside Dropbox and publish a closed, validated snapshot."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix(".lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with tempfile.TemporaryDirectory(prefix="econ-researcher-catalog-") as temporary:
                stage = Path(temporary) / "catalog.sqlite"
                if self.path.exists():
                    shutil.copy2(self.path, stage)
                connection = sqlite3.connect(stage)
                connection.row_factory = sqlite3.Row
                try:
                    connection.executescript(Path(__file__).with_name("catalog_schema.sql").read_text())
                    yield connection
                    connection.commit()
                    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise RuntimeError("Catalog integrity check failed; original database retained.")
                    if connection.execute("PRAGMA foreign_key_check").fetchall():
                        raise RuntimeError("Catalog foreign key check failed; original database retained.")
                    connection.close()
                    pending = self.path.with_name("." + self.path.name + "." + uuid.uuid4().hex + ".tmp")
                    try:
                        shutil.copy2(stage, pending)
                        pending.replace(self.path)
                    finally:
                        pending.unlink(missing_ok=True)
                finally:
                    connection.close()

    @contextmanager
    def read(self):
        connection = sqlite3.connect(self.path.as_uri() + "?mode=ro&immutable=1", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()

    def query(self, sql, parameters=()):
        with self.read() as connection:
            return [dict(r) for r in connection.execute(sql, parameters)]

    @staticmethod
    def audit(connection, action, details):
        connection.execute("INSERT INTO audit_events VALUES (?,?,?,?)", (str(uuid.uuid4()), now(), action, encode(details)))

    @staticmethod
    def _person(connection, name, orcid=None, profile_url=None, source_identity=None, linked_identities=()):
        orcid = normalize_orcid(orcid) if orcid else None
        by_id = connection.execute("SELECT researcher_id FROM people WHERE orcid=?", (orcid,)).fetchone() if orcid else None
        by_url = connection.execute("SELECT researcher_id,orcid FROM people WHERE profile_url=?", (profile_url,)).fetchone() if profile_url else None
        sources = ([source_identity] if source_identity else []) + list(linked_identities)
        by_sources = [row for identity in sources if (row := connection.execute("""SELECT p.researcher_id,p.orcid FROM people p
            JOIN person_identifiers i USING(researcher_id) WHERE i.scheme=? AND i.value=?""", identity).fetchone())]
        if len({row[0] for row in (by_id, by_url, *by_sources) if row}) > 1:
            raise ValueError("This ORCID and profile belong to different catalog identities; review them before merging.")
        if by_url and by_url[1] and orcid and by_url[1] != orcid:
            raise ValueError("The official profile has conflicting ORCID claims; identity needs review.")
        if any(row[1] and orcid and row[1] != orcid for row in by_sources):
            raise ValueError("The directory entry has a conflicting ORCID; identity needs review.")
        row = by_id or by_url or next(iter(by_sources), None)
        source_seed = encode(source_identity) if source_identity else None
        researcher_id = row[0] if row else stable_id("person:" + (orcid or profile_url or source_seed or str(uuid.uuid4())))
        connection.execute("""INSERT INTO people VALUES (?,?,?,?,?) ON CONFLICT(researcher_id) DO UPDATE SET
            orcid=COALESCE(people.orcid,excluded.orcid), profile_url=COALESCE(people.profile_url,excluded.profile_url)""",
            (researcher_id, name.strip(), orcid, profile_url, now()))
        return researcher_id

    def add_person(self, name, orcid=None):
        if not name.strip():
            raise ValueError("A researcher name is required.")
        with self.transaction() as connection:
            researcher_id = self._person(connection, name, orcid)
            self.audit(connection, "add_person", dict(researcher_id=researcher_id, name=name, orcid=orcid))
        return researcher_id

    def set_orcid(self, researcher_id, orcid, note):
        orcid = normalize_orcid(orcid)
        if not note.strip():
            raise ValueError("Describe how the identifier was verified.")
        with self.transaction() as connection:
            person = connection.execute("SELECT orcid FROM people WHERE researcher_id=?", (researcher_id,)).fetchone()
            if not person:
                raise ValueError("Researcher not found.")
            if person[0] and person[0] != orcid and connection.execute(
                    "SELECT 1 FROM assessments WHERE researcher_id=? LIMIT 1", (researcher_id,)).fetchone():
                raise ValueError("This identity already has assessments under another ORCID. Add the correct identity separately and review its memberships; existing results are retained.")
            owner = connection.execute("SELECT researcher_id FROM people WHERE orcid=?", (orcid,)).fetchone()
            if owner and owner[0] != researcher_id:
                raise ValueError("This ORCID is already assigned to another researcher; review the identities.")
            connection.execute("UPDATE people SET orcid=? WHERE researcher_id=?", (orcid, researcher_id))
            connection.execute("DELETE FROM person_identifiers WHERE researcher_id=? AND scheme='orcid'", (researcher_id,))
            connection.execute("INSERT INTO person_identifiers VALUES (?,?,?,?)",
                (researcher_id, "orcid", orcid, encode(dict(method="manual_review",note=note))))
            self.audit(connection, "set_orcid", dict(researcher_id=researcher_id, orcid=orcid, note=note))

    def add_organization(self, name, short_name, kind, url=""):
        if not name.strip() or not short_name.strip():
            raise ValueError("An organization name and short name are required.")
        identifier = stable_id("organization:" + key(name))
        with self.transaction() as connection:
            connection.execute("INSERT OR IGNORE INTO organizations VALUES (?,?,?,?,?,?,?)",
                (identifier, name.strip(), short_name.strip(), kind, url.strip(), None, "{}"))
            self.audit(connection, "add_organization", dict(organization_id=identifier, name=name))
        return identifier

    def set_membership(self, researcher_id, organization_id, membership_type, status, note, evidence=None):
        if not membership_type.strip():
            raise ValueError("A membership type is required.")
        if status not in {"listed", "confirmed", "review", "former"}:
            raise ValueError("Invalid membership status.")
        if not note.strip():
            raise ValueError("A membership review requires a note or source.")
        identifier = stable_id("affiliation:" + researcher_id + ":" + organization_id)
        with self.transaction() as connection:
            connection.execute("""INSERT INTO affiliations VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(researcher_id,organization_id) DO UPDATE SET membership_type=excluded.membership_type,
                status=excluded.status, checked_on=excluded.checked_on, note=excluded.note""",
                (identifier, researcher_id, organization_id, membership_type, status, dt.date.today().isoformat(),
                 note, encode(evidence or {"provider": "manual_review", "note": note})))
            self.audit(connection, "review_membership", dict(researcher_id=researcher_id, organization_id=organization_id,
                membership_type=membership_type, status=status, note=note))

    def import_roster(self, bundle):
        organization = bundle["organization"]
        provider = bundle.get("provider", organization["organization_id"] + "_official_roster")
        scheme = "roster:" + organization["organization_id"]
        with self.transaction() as connection:
            for org in (bundle.get("parent"), organization):
                if org is None:
                    continue
                connection.execute("INSERT OR IGNORE INTO organizations VALUES (?,?,?,?,?,?,?)", (
                    org["organization_id"], org["name"], org["short_name"], org["kind"], org["url"], org["parent_id"], encode(org["identifiers"])))
            for person in bundle["people"]:
                source_identity = (scheme, person["source_key"]) if person.get("source_key") else None
                identity_links = person.get("identity_links", [])
                try:
                    if any(not link.get("method") or not link.get("source_urls") for link in identity_links):
                        raise ValueError("Cross-directory identity links require a review method and supporting sources.")
                    researcher_id = self._person(connection, person["name"], person.get("orcid"), person.get("profile_url"),
                        source_identity, [(link["scheme"], link["value"]) for link in identity_links])
                except ValueError as error:
                    self.audit(connection, "roster_identity_conflict", dict(profile_url=person.get("profile_url"),
                        provider=provider, source_key=person.get("source_key"),
                        claimed_orcid=person.get("orcid"), detail=str(error)))
                    continue
                evidence = dict(provider=provider, source_url=bundle["source_url"],
                    profile_url=person.get("profile_url"), source_key=person.get("source_key"),
                    retrieved_at=bundle["retrieved_at"], raw_file=person.get("raw_file"), source_sha256=bundle.get("source_sha256"),
                    source_category=person.get("source_category"), listed_affiliation=person.get("listed_affiliation"),
                    homepages=person.get("homepages", []), additional_roles=person.get("additional_roles", []),
                    source_entries=person.get("source_entries", []),
                    research_lines=person.get("research_lines", []), warnings=person.get("warnings", []))
                if identity_links:
                    evidence["identity_links"] = identity_links
                if source_identity:
                    connection.execute("INSERT OR IGNORE INTO person_identifiers VALUES (?,?,?,?)",
                        (researcher_id, *source_identity, encode(evidence)))
                    claim = dict(evidence, organization_name=organization["short_name"],
                        interpretation="Listed on the official directory; current membership is not individually reviewed.")
                    claim_id = stable_id("claim:" + researcher_id + ":" + encode(claim))
                    connection.execute("INSERT OR IGNORE INTO affiliation_claims VALUES (?,?,?,?,?,?,?)",
                        (claim_id,researcher_id,organization["short_name"],provider,bundle["source_url"],bundle["retrieved_at"],encode(claim)))
                identifier = stable_id("affiliation:" + researcher_id + ":" + bundle["organization"]["organization_id"])
                connection.execute("INSERT OR IGNORE INTO affiliations VALUES (?,?,?,?,?,?,?,?)", (
                    identifier, researcher_id, bundle["organization"]["organization_id"], person["membership_type"],
                    "listed", bundle["as_of"], f"Listed on {organization['short_name']}'s official directory; not yet individually reviewed.", encode(evidence)))
                # Reviewed links are also retained when the other directory is
                # imported later, so a fresh catalog does not depend on roster order.
                for item in person.get("identifiers", []) + identity_links:
                    if item["scheme"] == "orcid" and item["value"] != person.get("orcid"):
                        continue
                    existing = connection.execute("SELECT researcher_id FROM person_identifiers WHERE scheme=? AND value=?",
                        (item["scheme"], item["value"])).fetchone()
                    if existing and existing[0] != researcher_id:
                        continue
                    connection.execute("INSERT OR IGNORE INTO person_identifiers VALUES (?,?,?,?)",
                        (researcher_id, item["scheme"], item["value"], encode(item)))
            self.audit(connection, "import_roster", dict(source=bundle["source_url"], as_of=bundle["as_of"], people=len(bundle["people"])))

    def import_assessment(self, bundle, source_path=""):
        return self.import_assessments([(bundle, source_path)])[0]

    def import_assessments(self, assessments):
        """Import a batch in one validated atomic catalog transaction."""
        assessments = list(assessments)
        if not assessments:
            return []
        if not all(uses_current_repec_policy(bundle) for bundle, _ in assessments):
            raise ValueError("Regenerate this assessment with the current recursive-IF scoring policy before importing.")
        if any(p['state'] == 'included' and p['reason'] != 'manual_override' and is_editorial_material(p)
                for bundle, _ in assessments for p in bundle['publications']):
            raise ValueError('Recalculate this assessment with the current editorial-material exclusion policy before importing.')
        with self.transaction() as connection:
            return [self._import_assessment(connection, bundle, path) for bundle, path in assessments]

    def _import_assessment(self, connection, bundle, source_path):
        researcher_id = self._person(connection, bundle["name"], bundle["orcid"])
        existing = connection.execute("SELECT bundle_json FROM assessments WHERE assessment_id=?", (bundle["run_id"],)).fetchone()
        if existing:
            if json.loads(existing[0]) != bundle:
                raise ValueError("This run identifier already contains a different assessment. Regenerate the assessment to create a new run.")
            return researcher_id
        policy = bundle["policy"]
        connection.execute("INSERT OR IGNORE INTO assessments VALUES (?,?,?,?,?,?,?,?,?,?)", (
            bundle["run_id"], researcher_id, bundle["as_of"], now(), bundle["rankings"]["sha256"], policy["year_policy"],
            policy.get("from_year"), policy.get("to_year"), str(source_path), encode(bundle)))
        # Explicit columns prevent source schema changes from shifting values.
        for p in bundle["publications"]:
            doi = normalize_doi(p.get("doi"))
            work_id = stable_id("doi:" + doi) if doi else stable_id("doi-less:" + researcher_id + ":" + p["publication_id"])
            connection.execute("INSERT OR IGNORE INTO works VALUES (?,?,?,?,?,?)", (
                work_id, doi, p["title"], p["journal_title"], p["journal_id"], p["kind"]))
            if p["journal_id"] is not None:
                connection.execute("UPDATE works SET journal_id=? WHERE work_id=? AND journal_id IS NULL",
                    (p["journal_id"], work_id))
            connection.execute("INSERT OR IGNORE INTO person_works VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                bundle["run_id"], researcher_id, work_id, p["publication_year"], p["state"], p["author_count"],
                p["fractional_credit"], p["repec_recursive_if_all"], p["fractional_repec_recursive_if_all"], p["ais"],
                p["abs_grade"], p["abdc_grade"], encode(p)))
        self.audit(connection, "import_assessment", dict(researcher_id=researcher_id, assessment_id=bundle["run_id"], source_path=str(source_path)))
        return researcher_id

    def add_claims(self, researcher_id, claims):
        with self.transaction() as connection:
            for claim in claims:
                identifier = stable_id("claim:" + researcher_id + ":" + encode(claim))
                connection.execute("INSERT OR IGNORE INTO affiliation_claims VALUES (?,?,?,?,?,?,?)", (
                    identifier, researcher_id, claim["organization_name"], claim["provider"], claim["source_url"],
                    claim.get("retrieved_at", now()), encode(claim)))
            self.audit(connection, "add_affiliation_evidence", dict(researcher_id=researcher_id, claims=len(claims)))

    def people(self):
        return self.query("SELECT * FROM people ORDER BY name COLLATE NOCASE")

    def organizations(self):
        return self.query("SELECT * FROM organizations ORDER BY short_name")

    def memberships(self, organization_id=None):
        return self.query("""SELECT p.*, a.affiliation_id, a.organization_id, a.membership_type, a.status,
            a.checked_on, a.note, a.evidence_json, o.short_name FROM affiliations a
            JOIN people p USING(researcher_id) JOIN organizations o USING(organization_id)"""
            + (" WHERE a.organization_id=?" if organization_id else "") + " ORDER BY p.name COLLATE NOCASE",
            (organization_id,) if organization_id else ())

    def latest_assessments(self, *, rankings_sha=None, year_policy=None, unrestricted=False, researcher_id=None):
        """Select the latest compatible score before choosing a run for each person."""
        conditions = ["json_extract(bundle_json,'$.policy.repec_summary.metric_id')=?",
                      "json_extract(bundle_json,'$.policy.repec_summary.horizon')=?"]
        parameters = [REPEC_SCORE_METRIC, REPEC_SCORE_HORIZON]
        for column, value in (("ranking_sha256", rankings_sha), ("year_policy", year_policy),
                              ("researcher_id", researcher_id)):
            if value is not None:
                conditions.append(column + "=?")
                parameters.append(value)
        if unrestricted:
            conditions.extend(["from_year IS NULL", "to_year IS NULL"])
        return self.query("""SELECT * FROM (SELECT a.*, ROW_NUMBER() OVER
            (PARTITION BY researcher_id ORDER BY as_of DESC,imported_at DESC,assessment_id DESC) AS n
            FROM assessments a WHERE """ + " AND ".join(conditions) + ") WHERE n=1", parameters)

    def comparison(self, organization_id, *, from_year=None, to_year=None, membership_types=None,
                   reviewed_only=False, rankings_sha=None, year_policy="bibliographic"):
        if from_year and to_year and from_year > to_year:
            raise ValueError("The first publication year must not exceed the last.")
        memberships = [m for m in self.memberships(organization_id)
            if m["status"] in ({"confirmed"} if reviewed_only else ACTIVE_STATUSES)
            and (membership_types is None or m["membership_type"] in membership_types)]
        ranking_sha = rankings_sha or ranking_digest()
        latest = self.latest_assessments(rankings_sha=ranking_sha, year_policy=year_policy, unrestricted=True)
        assessments = {a["researcher_id"]: a for a in latest}
        rows, papers, dates = [], [], set()
        for member in memberships:
            assessment = assessments.get(member["researcher_id"])
            selected = []
            if assessment:
                bundle = json.loads(assessment["bundle_json"])
                selected = self.query("""SELECT pw.*,w.doi,w.title,w.journal_title FROM person_works pw JOIN works w USING(work_id)
                    WHERE assessment_id=? AND state='included'""", (assessment["assessment_id"],))
                selected = [p for p in selected if p["publication_year"] is not None
                    and (from_year is None or p["publication_year"] >= from_year)
                    and (to_year is None or p["publication_year"] <= to_year)]
                dates.add(assessment["as_of"])
                papers.extend(dict(p, name=member["name"]) for p in selected)
            row = dict(researcher_id=member["researcher_id"], name=member["name"], orcid=member["orcid"],
                membership_type=member["membership_type"], membership_status=member["status"],
                assessment_as_of=assessment["as_of"] if assessment else None,
                publications=len(selected) if assessment else None,
                fractional_publications=partial_sum([p["fractional_credit"] for p in selected]) if assessment else None,
                recursive_full=partial_sum([p["recursive_if"] for p in selected]) if assessment else None,
                recursive_fractional=partial_sum([p["fractional_recursive_if"] for p in selected]) if assessment else None,
                recursive_coverage=sum(p["recursive_if"] is not None for p in selected),
                fractional_coverage=sum(p["fractional_recursive_if"] is not None for p in selected),
                ais_sum=partial_sum([p["ais"] for p in selected]) if assessment else None,
                fractional_ais_sum=partial_sum([p["ais"] * p["fractional_credit"]
                    if p["ais"] is not None and p["fractional_credit"] is not None else None
                    for p in selected]) if assessment else None,
                ais_coverage=sum(p["ais"] is not None for p in selected),
                fractional_ais_coverage=sum(p["ais"] is not None and p["fractional_credit"] is not None for p in selected),
                abs_distribution=dict(Counter(p["abs_grade"] or "unavailable" for p in selected)),
                abdc_distribution=dict(Counter(p["abdc_grade"] or "unavailable" for p in selected)),
                review_cases=sum(p["state"] == "review" and (p["publication_year"] is None or
                    ((from_year is None or p["publication_year"] >= from_year) and (to_year is None or p["publication_year"] <= to_year)))
                    for p in bundle["publications"]) if assessment else None,
                discovery_status=discovery_status(bundle) if assessment else "Not collected")
            row["source_coverage"] = publication_coverage(bundle) if assessment else None
            rows.append(row)
        groups = defaultdict(list)
        for p in papers:
            groups[p["work_id"]].append(p)
        unique_papers, issues = [], []
        for work_id, group in groups.items():
            factors = {p["recursive_if"] for p in group if p["recursive_if"] is not None}
            factor = next(iter(factors)) if len(factors) == 1 else None
            ais_values = {p["ais"] for p in group if p["ais"] is not None}
            ais = next(iter(ais_values)) if len(ais_values) == 1 else None
            counts = {p["author_count"] for p in group if p["author_count"] is not None}
            credits = [p["fractional_credit"] for p in group]
            fraction = None
            if all(c is not None for c in credits) and len(counts) == 1 and fsum(credits) <= 1 + 1e-9:
                fraction = fsum(credits)
            if len(factors) > 1 or len(ais_values) > 1 or len(counts) > 1 or (all(c is not None for c in credits) and fsum(credits) > 1 + 1e-9):
                issues.append("Conflicting journal weights or author counts for " + group[0]["title"])
            p = group[0]
            unique_papers.append(dict(work_id=work_id, doi=p["doi"], title=p["title"], journal_title=p["journal_title"],
                publication_year=p["publication_year"], members=sorted(p["name"] for p in group),
                member_ids=sorted(p["researcher_id"] for p in group), recursive_if=factor,
                fractional_credit=fraction, fractional_recursive_if=factor * fraction if factor is not None and fraction is not None else None,
                ais=ais, fractional_ais=ais * fraction if ais is not None and fraction is not None else None))
        assessed = sum(r["publications"] is not None for r in rows)
        summary = dict(members=len(rows), assessed_members=assessed, unique_publications=len(unique_papers) if assessed else None,
            fractional_publications=partial_sum([p["fractional_credit"] for p in unique_papers]) if assessed else None,
            fractional_publications_coverage=sum(p["fractional_credit"] is not None for p in unique_papers),
            recursive_full=partial_sum([p["recursive_if"] for p in unique_papers]) if assessed else None,
            recursive_fractional=partial_sum([p["fractional_recursive_if"] for p in unique_papers]) if assessed else None,
            recursive_coverage=sum(p["recursive_if"] is not None for p in unique_papers),
            fractional_coverage=sum(p["fractional_recursive_if"] is not None for p in unique_papers),
            ais_sum=partial_sum([p["ais"] for p in unique_papers]) if assessed else None,
            fractional_ais_sum=partial_sum([p["fractional_ais"] for p in unique_papers]) if assessed else None,
            ais_coverage=sum(p["ais"] is not None for p in unique_papers),
            fractional_ais_coverage=sum(p["fractional_ais"] is not None for p in unique_papers),
            doi_less_records=sum(not p["doi"] for p in unique_papers))
        for metric, ratio in (("unique_publications", "publications_per_member"),
                ("fractional_publications", "fractional_publications_per_member"),
                ("recursive_full", "recursive_full_per_member"),
                ("recursive_fractional", "recursive_fractional_per_member"),
                ("ais_sum", "ais_per_member"), ("fractional_ais_sum", "fractional_ais_per_member")):
            summary[ratio] = summary[metric] / len(rows) if summary[metric] is not None and rows else None
        return dict(policy=dict(attribution="current_members_carry_selected_publication_portfolio", from_year=from_year,
            to_year=to_year, year_policy=year_policy, ranking_sha256=ranking_sha, metric=REPEC_SCORE_METRIC,
            horizon=REPEC_SCORE_HORIZON, reviewed_only=reviewed_only, membership_types=membership_types,
            editorial_material='excluded',
            scope_sources=["jcr", "abs", "abdc", "reviewed_publisher_classification"],
            per_member_denominator="all_selected_current_members",
            publication_sources=publication_source_policy()),
            organization_id=organization_id, summary=summary, researchers=rows,
            publications=sorted(unique_papers, key=lambda p: (p["publication_year"] or 0,p["title"]), reverse=True),
            assessment_dates=sorted(dates), issues=issues, membership_snapshot=memberships)


def partial_sum(values):
    available = [v for v in values if v is not None]
    return fsum(available) if available else (None if values else 0.0)


def discovery_status(bundle):
    return "ORCID: " + publication_coverage(bundle)["primary_label"]


def latest_roster_paths(root=None):
    latest = {}
    for path in sorted(((root or ROOT) / "data/organizations").glob("*/*/roster.json")):
        latest[path.parent.parent.name] = path
    return list(latest.values())


def bootstrap(catalog):
    """Import available snapshots once; never reset reviewed memberships on reruns."""
    with catalog.read() as connection:
        empty = not connection.execute("SELECT 1 FROM people LIMIT 1").fetchone()
    if not empty:
        return
    for roster_path in latest_roster_paths():
        roster = json.loads(roster_path.read_text())
        catalog.import_roster(roster)
        reviews = roster_path.with_name("identifier_reviews.json")
        if reviews.exists():
            for review in json.loads(reviews.read_text())["reviews"]:
                # Replay documented identifier decisions before importing works.
                # Match the official profile URL, never a name alone.
                if review.get("source_key"):
                    matches = catalog.query("SELECT researcher_id FROM person_identifiers WHERE scheme=? AND value=?",
                        ("roster:" + roster["organization"]["organization_id"],review["source_key"]))
                else:
                    matches = [p for p in catalog.people() if p["profile_url"] == review["profile_url"]]
                if len(matches) != 1:
                    raise ValueError("Identifier review has no unique roster entry: " + str(review.get("profile_url") or review.get("source_key")))
                sources = [review.get(k) for k in ("source_url", "additional_source", "crosslink_source") if review.get(k)]
                catalog.set_orcid(matches[0]["researcher_id"], review["orcid"],
                                  review["method"] + " Sources: " + "; ".join(sources))
    for path in sorted((ROOT / "data/researchers").glob("*/*/assessment.json")):
        bundle = json.loads(path.read_text())
        if uses_current_repec_policy(bundle):
            catalog.import_assessment(bundle, path)
