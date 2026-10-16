"""Attach private AIS observations to a read-only hosted catalog at runtime."""
import hashlib
import json
import math
from pathlib import Path
import shutil
import sqlite3
import tempfile

from .catalog import encode
from .core import summarize
from .publishing import PublishedCatalog


AIS_SECRET = "ECON_RANKINGS_PRIVATE_AIS"


def private_ais_payload(source, snapshot, *, rankings_sha):
    """Export only observations belonging to the exact published assessments."""
    published = PublishedCatalog(snapshot)
    assessments = {row["assessment_id"]: row for row in source.latest_assessments(
        rankings_sha=rankings_sha, year_policy="bibliographic", unrestricted=True)}
    values = {}
    for row in published.latest_assessments():
        if row["assessment_id"] not in assessments:
            raise ValueError("Rebuild the public snapshot before exporting private AIS observations.")
        bundle = json.loads(assessments[row["assessment_id"]]["bundle_json"])
        for publication in bundle["publications"]:
            value = publication.get("ais")
            if value is None:
                continue
            identifier = publication["publication_id"]
            if identifier in values and values[identifier] != value:
                raise ValueError("Conflicting AIS observations for a publication; review before deployment.")
            values[identifier] = value
    return dict(version=1, snapshot_sha256=hashlib.sha256(Path(snapshot).read_bytes()).hexdigest(),
        ais_by_publication=values)


class PrivateAisCatalog(PublishedCatalog):
    """Keep AIS outside Git and leave the committed snapshot unchanged."""
    def __init__(self, snapshot, payload):
        snapshot = Path(snapshot).resolve()
        if payload.get("version") != 1 or payload.get("snapshot_sha256") != hashlib.sha256(snapshot.read_bytes()).hexdigest():
            raise ValueError("Private AIS observations do not match this snapshot; update the app's AIS secret.")
        values = payload.get("ais_by_publication")
        if not isinstance(values, dict) or any(not isinstance(identifier, str) or
                isinstance(value, bool) or not isinstance(value, (int, float)) or
                not math.isfinite(value) or value < 0 for identifier, value in values.items()):
            raise ValueError("Private AIS observations must contain finite, non-negative scores.")
        self._temporary = tempfile.TemporaryDirectory(prefix="econ-private-ais-")
        path = Path(self._temporary.name) / "catalog.sqlite"
        try:
            shutil.copy2(snapshot, path)
            with sqlite3.connect(path) as connection:
                rows = connection.execute("SELECT assessment_id,work_id,publication_json FROM person_works").fetchall()
                updates = []
                identifiers = set()
                for assessment_id, work_id, encoded in rows:
                    publication = json.loads(encoded)
                    identifier = publication["publication_id"]
                    identifiers.add(identifier)
                    publication["ais"] = values.get(identifier)
                    updates.append((publication["ais"], encode(publication), assessment_id, work_id))
                if set(values) - identifiers:
                    raise ValueError("Private AIS observations contain publications absent from this snapshot.")
                connection.executemany("UPDATE person_works SET ais=?,publication_json=? WHERE assessment_id=? AND work_id=?", updates)
                updates = []
                for assessment_id, encoded in connection.execute("SELECT assessment_id,bundle_json FROM assessments").fetchall():
                    bundle = json.loads(encoded)
                    for publication in bundle["publications"]:
                        publication["ais"] = values.get(publication["publication_id"])
                    bundle["summary"] = summarize(bundle["publications"])
                    bundle["policy"]["published_metrics"] = ["publications", "repec.recurse.all", "jcr.ais"]
                    updates.append((encode(bundle), assessment_id))
                connection.executemany("UPDATE assessments SET bundle_json=? WHERE assessment_id=?", updates)
            super().__init__(path)
        except Exception:
            self._temporary.cleanup()
            raise
