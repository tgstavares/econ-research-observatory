"""Combine the presentation of independently assessed current-affiliation groups."""
from collections import Counter


def comparison_researchers(comparisons, names):
    """List each person once without adding their scores across affiliations."""
    policy_fields = ("from_year", "to_year", "year_policy", "ranking_sha256", "metric", "horizon", "editorial_material")
    policies = {tuple(c["policy"].get(k) for k in policy_fields) for c in comparisons.values()}
    if len(policies) > 1:
        raise ValueError("Groups must use the same publication years, journal snapshot and scoring policy.")
    score_fields = ("publications", "fractional_publications", "recursive_full", "recursive_fractional",
        "ais_sum", "fractional_ais_sum", "recursive_coverage", "fractional_coverage", "ais_coverage",
        "fractional_ais_coverage", "assessment_as_of", "review_cases")
    people = {}
    for group_id, comparison in comparisons.items():
        for researcher in comparison["researchers"]:
            identifier = researcher["researcher_id"]
            if identifier not in people:
                people[identifier] = dict(researcher, groups=[], group_ids=[], group_memberships=[])
            row = people[identifier]
            if any(row.get(k) != researcher.get(k) for k in score_fields):
                raise ValueError("Inconsistent individual scores across groups for " + researcher["name"] + ".")
            row["groups"].append(names[group_id])
            row["group_ids"].append(group_id)
            row["group_memberships"].append(dict(organization_id=group_id,
                membership_type=researcher["membership_type"], membership_status=researcher["membership_status"]))
    return list(people.values())


def comparison_overlap(comparisons):
    """Count overlapping identities; no institution scores are pooled."""
    researchers, papers = Counter(), Counter()
    for comparison in comparisons.values():
        researchers.update({r["researcher_id"] for r in comparison["researchers"]})
        papers.update({p["work_id"] for p in comparison["publications"]})
    return dict(researchers_in_multiple_groups=sum(n > 1 for n in researchers.values()),
        publications_in_multiple_groups=sum(n > 1 for n in papers.values()))
