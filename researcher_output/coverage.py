"""Distinguish primary ORCID retrieval from supplementary source coverage."""

SOURCE_LABELS = {"orcid": "ORCID", "crossref": "Crossref", "openalex": "OpenAlex",
                 "reviewed_institutional": "Reviewed institutional records"}
SUPPLEMENTARY_SOURCES = ("crossref", "openalex")


def publication_source_policy():
    return dict(primary="orcid", primary_visibility="public",
        supplementary=[*SUPPLEMENTARY_SOURCES, "reviewed_institutional"],
        supplementary_publications="retain_independently_verified_authorships",
        metadata_preference=["crossref", "orcid", "openalex", "reviewed_institutional"],
        preserve_primary_records_on_supplementary_failure=True)


def publication_coverage(bundle):
    sources = {c["provider"]: c for c in bundle.get("coverage", [])}
    primary = sources.get("orcid", {})
    status = primary.get("status", "not_recorded")
    complete = status == "complete_public_section"
    if complete:
        groups = primary.get("groups")
        label = "Retrieved" + (f" ({groups} listed {'work' if groups == 1 else 'works'})" if groups is not None else "")
    else:
        label = {"unavailable": "Unavailable", "partial": "Partially retrieved"}.get(status, "Not recorded")
    supplements, issues = [], []
    for provider in SUPPLEMENTARY_SOURCES:
        source_status = sources.get(provider, {}).get("status", "not_recorded")
        retrieved = source_status.startswith("complete")
        source_label = "retrieved" if retrieved else {
            "unavailable": "unavailable", "partial": "partial"}.get(source_status, "not recorded")
        supplements.append(f"{SOURCE_LABELS[provider]} {source_label}")
        if not retrieved:
            issues.append(provider)
    if reviewed := sources.get("reviewed_institutional"):
        supplements.append(f"Reviewed institutional records verified ({reviewed['records']})")
    return dict(primary_source="orcid", primary_status=status, primary_complete=complete,
        primary_groups=primary.get("groups"), primary_label=label,
        supplementary_status="; ".join(supplements), supplementary_issues=issues)


def coverage_rows(bundle):
    return [dict(c, role="Primary" if c["provider"] == "orcid" else "Supplementary")
        for c in bundle.get("coverage", [])]
