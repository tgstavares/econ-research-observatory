"""Create a compact publication snapshot and enforce read-only public access."""
from contextlib import contextmanager
import datetime as dt
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile

from .catalog import ResearcherCatalog, encode
from .core import repec_summary_policy, summarize


PUBLIC_FIELDS = ("publication_id", "title", "doi", "journal_title", "journal_id", "kind",
    "publication_year", "author_count", "fractional_credit", "repec_recursive_if_all",
    "fractional_repec_recursive_if_all", "repec_rank_all", "state", "reason", "repec_note")
PUBLIC_METRICS = ["publications", "repec.recurse.all"]


def public_export(value):
    """Keep withheld metric placeholders out of public JSON downloads."""
    if isinstance(value, dict):
        return {key: public_export(item) for key, item in value.items()
            if not {"ais", "abs", "abdc"}.intersection(key.lower().split("_"))}
    if isinstance(value, list):
        return [public_export(item) for item in value]
    return value


class PublishedCatalog(ResearcherCatalog):
    def __init__(self, path):
        self.path = Path(path).resolve()
        if not self.path.is_file():
            raise FileNotFoundError("The published snapshot is missing; rebuild the deployment package.")

    @contextmanager
    def transaction(self):
        raise PermissionError("The public catalog is read-only.")
        yield  # Keep the context-manager contract without opening a writable connection.


def public_publication(publication):
    result = {key: publication.get(key) for key in PUBLIC_FIELDS}
    # Null placeholders preserve the shared calculation schema, without publishing
    # licensed observations, grades, their source rows, or their derived totals.
    result.update(ais=None, abs_grade=None, abdc_grade=None)
    return result


def public_bundle(bundle):
    publications = [public_publication(p) for p in bundle["publications"]]
    return dict(run_id=bundle["run_id"], name=bundle["name"], orcid=bundle["orcid"], as_of=bundle["as_of"],
        rankings=dict(sha256=bundle["rankings"]["sha256"], path="published-snapshot"),
        policy=dict(year_policy="bibliographic", from_year=None, to_year=None,
            repec_summary=repec_summary_policy(), published_metrics=PUBLIC_METRICS),
        summary=summarize(publications), publications=publications,
        coverage=[{key: source[key] for key in ("provider", "status", "records", "groups") if key in source}
            for source in bundle.get("coverage", [])], warnings=[])


def export_snapshot(source, destination, *, rankings_sha, published_on=None):
    """Export latest compatible assessments with an explicit data allowlist."""
    destination = Path(destination).resolve()
    if destination == source.path:
        raise ValueError("A published snapshot must be separate from the maintenance database.")
    latest = source.latest_assessments(rankings_sha=rankings_sha, year_policy="bibliographic", unrestricted=True)
    if not latest:
        raise ValueError("No compatible assessments are available for publication.")
    works = {work["work_id"]: work for work in source.query("SELECT * FROM works")}
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="econ-public-snapshot-") as temporary:
        staged = Path(temporary) / "snapshot.sqlite"
        with sqlite3.connect(staged) as connection:
            connection.executescript(Path(__file__).with_name("catalog_schema.sql").read_text())
            connection.execute("PRAGMA defer_foreign_keys=ON")
            for organization in source.organizations():
                connection.execute("INSERT INTO organizations VALUES (?,?,?,?,?,?,?)", (
                    organization["organization_id"], organization["name"], organization["short_name"],
                    organization["kind"], organization["url"], organization["parent_id"], "{}"))
            for person in source.people():
                connection.execute("INSERT INTO people VALUES (?,?,?,?,?)", tuple(person[key] for key in
                    ("researcher_id", "name", "orcid", "profile_url", "created_at")))
            for member in source.memberships():
                connection.execute("INSERT INTO affiliations VALUES (?,?,?,?,?,?,?,?)", (
                    member["affiliation_id"], member["researcher_id"], member["organization_id"],
                    member["membership_type"], member["status"], member["checked_on"], "",
                    encode(dict(profile_url=member["profile_url"]))))
            for assessment in latest:
                bundle = public_bundle(json.loads(assessment["bundle_json"]))
                connection.execute("INSERT INTO assessments VALUES (?,?,?,?,?,?,?,?,?,?)", (
                    assessment["assessment_id"], assessment["researcher_id"], assessment["as_of"],
                    assessment["imported_at"], rankings_sha, "bibliographic", None, None,
                    "published-snapshot", encode(bundle)))
                for row in source.query("SELECT * FROM person_works WHERE assessment_id=?", (assessment["assessment_id"],)):
                    work = works[row["work_id"]]
                    connection.execute("INSERT OR IGNORE INTO works VALUES (?,?,?,?,?,?)", tuple(work[key] for key in
                        ("work_id", "doi", "title", "journal_title", "journal_id", "kind")))
                    publication = public_publication(json.loads(row["publication_json"]))
                    connection.execute("INSERT INTO person_works VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                        row["assessment_id"], row["researcher_id"], row["work_id"], row["publication_year"],
                        row["state"], row["author_count"], row["fractional_credit"], row["recursive_if"],
                        row["fractional_recursive_if"], None, None, None, encode(publication)))
            connection.commit()
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or connection.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError("Public snapshot integrity validation failed.")
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".public-", suffix=".sqlite", delete=False) as pending:
            pending.write(staged.read_bytes())
            pending_path = Path(pending.name)
        try:
            pending_path.replace(destination)
        finally:
            pending_path.unlink(missing_ok=True)
    return dict(version=1, published_on=published_on or dt.date.today().isoformat(), ranking_date="2026-10-14",
        ranking_sha256=rankings_sha, database_sha256=hashlib.sha256(destination.read_bytes()).hexdigest(),
        metrics=PUBLIC_METRICS, withheld_metrics=["jcr.ais", "abs.rating", "abdc.rating"],
        researchers=len(source.people()), assessments=len(latest), read_only=True,
        attribution="current_members_carry_selected_publication_portfolio")
