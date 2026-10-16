PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY, name TEXT NOT NULL, short_name TEXT NOT NULL,
    kind TEXT NOT NULL, url TEXT NOT NULL DEFAULT '', parent_id TEXT REFERENCES organizations,
    identifiers_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS people (
    researcher_id TEXT PRIMARY KEY, name TEXT NOT NULL, orcid TEXT UNIQUE,
    profile_url TEXT UNIQUE, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS person_identifiers (
    researcher_id TEXT NOT NULL REFERENCES people, scheme TEXT NOT NULL, value TEXT NOT NULL,
    evidence_json TEXT NOT NULL, PRIMARY KEY(scheme,value)
);
CREATE TABLE IF NOT EXISTS affiliations (
    affiliation_id TEXT PRIMARY KEY, researcher_id TEXT NOT NULL REFERENCES people,
    organization_id TEXT NOT NULL REFERENCES organizations, membership_type TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('listed','confirmed','review','former')),
    checked_on TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', evidence_json TEXT NOT NULL,
    UNIQUE(researcher_id,organization_id)
);
CREATE TABLE IF NOT EXISTS affiliation_claims (
    claim_id TEXT PRIMARY KEY, researcher_id TEXT NOT NULL REFERENCES people,
    organization_name TEXT NOT NULL, provider TEXT NOT NULL, source_url TEXT NOT NULL,
    retrieved_at TEXT NOT NULL, evidence_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assessments (
    assessment_id TEXT PRIMARY KEY, researcher_id TEXT NOT NULL REFERENCES people,
    as_of TEXT NOT NULL, imported_at TEXT NOT NULL, ranking_sha256 TEXT NOT NULL,
    year_policy TEXT NOT NULL, from_year INTEGER, to_year INTEGER,
    source_path TEXT NOT NULL, bundle_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS works (
    work_id TEXT PRIMARY KEY, doi TEXT UNIQUE, title TEXT NOT NULL, journal_title TEXT,
    journal_id TEXT, kind TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS person_works (
    assessment_id TEXT NOT NULL REFERENCES assessments, researcher_id TEXT NOT NULL REFERENCES people,
    work_id TEXT NOT NULL REFERENCES works, publication_year INTEGER, state TEXT NOT NULL,
    author_count INTEGER, fractional_credit REAL, recursive_if REAL, fractional_recursive_if REAL,
    ais REAL, abs_grade TEXT, abdc_grade TEXT, publication_json TEXT NOT NULL,
    PRIMARY KEY(assessment_id,work_id)
);
CREATE INDEX IF NOT EXISTS person_works_researcher ON person_works(researcher_id);
CREATE TABLE IF NOT EXISTS audit_events (
    event_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, action TEXT NOT NULL, details_json TEXT NOT NULL
);
