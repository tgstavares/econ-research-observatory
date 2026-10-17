"""Private local browser app: .venv/bin/python -m streamlit run app.py"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import sys
from urllib.parse import urlencode

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from researcher_output.catalog import ResearcherCatalog, DEFAULT_DATABASE, DEFAULT_RANKINGS, ACTIVE_STATUSES, bootstrap, ranking_digest
from researcher_output.core import summarize
from researcher_output.coverage import publication_coverage, coverage_rows
from researcher_output.portfolio_charts import COMPARISON_METRICS, GROUP_COLORS, annual_group_output, lorenz_curve, annual_spec, lorenz_spec
from researcher_output.group_comparison import comparison_researchers, comparison_overlap
from researcher_output.publishing import PublishedCatalog, public_export
from researcher_output.private_metrics import AIS_SECRET, PrivateAisCatalog

PUBLIC_MODE = os.environ.get("ECON_RANKINGS_PUBLIC", "0") == "1"
SHOW_AIS = not PUBLIC_MODE
if not PUBLIC_MODE:
    from researcher_output.workflows import run_and_import, review_publication

st.set_page_config(page_title="Economics & Finance · Researcher Observatory", page_icon="◈", layout="wide")
st.html("""<style>.block-container{padding-top:2.6rem;max-width:1200px;width:100%;margin-inline:auto}h1{letter-spacing:-.035em}
[data-testid="stMetric"]{padding:10px 14px;background:#fff;border:1px solid #dce5df;border-radius:10px}
[data-testid="stMetricValue"]{font-size:1.85rem;line-height:1.15}
[data-testid="stMetricLabel"] p{font-size:.875rem;line-height:1.25;margin:0}
.st-key-portfolio_summary td:not(:first-child),.st-key-portfolio_summary th:not(:first-child){text-align:right!important;font-variant-numeric:tabular-nums}
.st-key-portfolio_group_summary td:nth-child(n+3),.st-key-portfolio_group_summary th:nth-child(n+3){text-align:right!important;font-variant-numeric:tabular-nums}
[data-testid="stSidebar"]{border-right:1px solid #dce5df}div[data-testid="stCaptionContainer"]{line-height:1.5}
</style>""")
if PUBLIC_MODE:
    public_manifest = json.loads(Path(os.environ.get("ECON_RANKINGS_PUBLIC_MANIFEST", ROOT / "data/manifest.json")).read_text())
    snapshot_path = os.environ.get("ECON_RANKINGS_DATABASE", ROOT / "data/public.sqlite")
    try:
        ais_secret = st.secrets.get(AIS_SECRET)
    except FileNotFoundError:
        ais_secret = None
    if ais_secret:
        @st.cache_resource(show_spinner=False)
        def private_hosted_catalog(path, payload_json, snapshot_sha256):
            return PrivateAisCatalog(path, json.loads(payload_json))
        try:
            store = private_hosted_catalog(snapshot_path, ais_secret, public_manifest["database_sha256"])
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            st.error(str(error))
            st.stop()
        SHOW_AIS = True
    else:
        store = PublishedCatalog(snapshot_path)
    snapshot_hash = public_manifest["ranking_sha256"]
    edition = "Hosted read-only snapshot"
    st.caption(f"{edition} · {public_manifest['published_on']} · Publications and RePEc recursive IF (all-period)" +
        (" · JCR AIS 2025" if SHOW_AIS else ""))
else:
    store = ResearcherCatalog(os.environ.get("ECON_RANKINGS_DATABASE", DEFAULT_DATABASE))
    if os.environ.get("ECON_RANKINGS_BOOTSTRAP", "1") != "0":
        bootstrap(store)
    snapshot_hash = ranking_digest()
people, organizations = store.people(), store.organizations()
org_names = {o["organization_id"]: o["short_name"] for o in organizations}
person_names = {p["researcher_id"]: p["name"] for p in people}
STATUS_LABELS = {"listed": "Listed by source", "confirmed": "Reviewed current", "review": "Needs review", "former": "No longer current"}


def number(value, places=3):
    return "—" if value is None else f"{value:,.{places}f}"


def organization_picker(key, *, current_members_only=False):
    ids = list(org_names)
    if current_members_only:
        active = {m["organization_id"] for m in store.memberships() if m["status"] in ACTIVE_STATUSES}
        ids = [identifier for identifier in ids if identifier in active]
    if key in st.session_state and st.session_state[key] not in ids:
        st.session_state.pop(key)
        if current_members_only:
            st.session_state.pop("comparison_types", None)
    if not ids:
        st.info("No organizations have current members. Add or review memberships in Memberships."
            if current_members_only else "Add an organization in Memberships to begin.")
        return None
    return st.selectbox("University or research center", ids, index=ids.index("nipe") if "nipe" in ids else 0,
                        format_func=org_names.get, key=key)


def person_picker(key):
    if not people:
        st.info("Add a researcher in Collect & import to begin.")
        return None
    ids = list(person_names)
    return st.selectbox("Researcher", ids, format_func=person_names.get, key=key)


def current_organization_ids():
    active = {m["organization_id"] for m in store.memberships() if m["status"] in ACTIVE_STATUSES}
    return [identifier for identifier in org_names if identifier in active]


def comparison_organization_picker():
    ids = current_organization_ids()
    if not ids:
        st.info("No organizations have current members. Add or review memberships in Memberships.")
        return []
    key = "comparison_organizations"
    if key in st.session_state:
        valid = [identifier for identifier in st.session_state[key] if identifier in ids]
        if valid != st.session_state[key]:
            st.session_state[key] = valid
    previous = st.session_state.get("comparison_organization")
    default = [previous] if previous in ids else ["nipe" if "nipe" in ids else ids[0]]
    selected = st.multiselect("Universities or research centers", ids, default=default,
        format_func=org_names.get, key=key)
    return [identifier for identifier in ids if identifier in selected]


def comparison_membership_picker(organization_id, *, multiple=False):
    memberships = store.memberships(organization_id)
    available_types = sorted({m["membership_type"] for m in memberships if m["status"] in ACTIVE_STATUSES})
    default_types = next(([kind] for kind in ("Full member", "Member") if kind in available_types), available_types)
    key = "comparison_types_" + organization_id
    if key in st.session_state:
        old = st.session_state[key]
        valid = [kind for kind in old if kind in available_types]
        if valid != old:
            st.session_state[key] = valid if valid or not old else default_types
    label = org_names[organization_id] + " membership categories" if multiple else "Membership categories"
    return st.multiselect(label, available_types, default=default_types, key=key)


def portfolio_summary_rows(summary):
    rows = [
        {"Measure":"Publications", "Total":number(summary["unique_publications"], 0),
            "Fractional total":number(summary["fractional_publications"]),
            "Per member":number(summary["publications_per_member"]),
            "Fractional/member":number(summary["fractional_publications_per_member"])},
        {"Measure":"RePEc recursive IF · all-period", "Total":number(summary["recursive_full"]),
            "Fractional total":number(summary["recursive_fractional"]),
            "Per member":number(summary["recursive_full_per_member"]),
            "Fractional/member":number(summary["recursive_fractional_per_member"])},
        {"Measure":"JCR AIS · 2025", "Total":number(summary["ais_sum"]),
            "Fractional total":number(summary["fractional_ais_sum"]),
            "Per member":number(summary["ais_per_member"]),
            "Fractional/member":number(summary["fractional_ais_per_member"])},
    ]
    return rows if SHOW_AIS else rows[:2]


def comparison_coverage(summary):
    coverage = f"IF coverage: {summary['recursive_coverage']}/{summary['unique_publications'] or 0} unique papers."
    if SHOW_AIS:
        coverage += f" AIS coverage: {summary['ais_coverage']}/{summary['unique_publications'] or 0} unique papers."
    if summary["fractional_coverage"] != summary["recursive_coverage"]:
        coverage += f" Fractional IF coverage: {summary['fractional_coverage']}/{summary['unique_publications'] or 0}."
    if SHOW_AIS and summary["fractional_ais_coverage"] != summary["ais_coverage"]:
        coverage += f" Fractional AIS coverage: {summary['fractional_ais_coverage']}/{summary['unique_publications'] or 0}."
    return coverage


def comparison_papers_table(comparison):
    st.dataframe(pd.DataFrame([{"Year":p["publication_year"], "Paper":p["title"], "Journal":p["journal_title"],
        "Members":"; ".join(p["members"]), "RePEc IF (all-period)":p["recursive_if"],
        "Fractional contribution":p["fractional_recursive_if"], "DOI":f"https://doi.org/{p['doi']}" if p["doi"] else None}
        for p in comparison["publications"]]), hide_index=True, width="stretch",
        column_config={"DOI":st.column_config.LinkColumn("DOI")})


def clear_profile_route():
    if st.session_state.get("workspace_page") != "Researchers":
        st.query_params.pop("researcher", None)
        st.session_state.pop("_profile_route", None)


def comparison_page():
    st.title("Compare publication portfolios")
    st.caption("Every selected publication follows the researcher to their current affiliations, regardless of where it was written.")
    organization_ids = comparison_organization_picker()
    if not organization_ids:
        if current_organization_ids():
            st.info("Select one or more groups to compare their publication portfolios.")
        return
    multiple = len(organization_ids) > 1
    if multiple:
        category_cols = st.columns(min(len(organization_ids), 3))
        kinds = {}
        for index, identifier in enumerate(organization_ids):
            with category_cols[index % len(category_cols)]:
                kinds[identifier] = comparison_membership_picker(identifier, multiple=True)
        year_cols = st.columns(2)
    else:
        cols = st.columns([2,1,1])
        with cols[0]:
            kinds = {organization_ids[0]: comparison_membership_picker(organization_ids[0])}
        year_cols = cols[1:]
    with year_cols[0]:
        first = st.number_input("From year", min_value=1900, max_value=dt.date.today().year, value=dt.date.today().year-4, step=1)
    with year_cols[1]:
        last = st.number_input("To year", min_value=1900, max_value=dt.date.today().year, value=dt.date.today().year, step=1)
    options = st.columns(2)
    with options[0]:
        all_years = st.checkbox("All publication years", key="all_publication_years")
    with options[1]:
        reviewed = st.checkbox("Reviewed memberships only", key="reviewed_memberships_only", help="Only memberships individually reviewed as current.")
    try:
        comparisons = {identifier: store.comparison(identifier, from_year=None if all_years else int(first),
            to_year=None if all_years else int(last), membership_types=kinds[identifier], reviewed_only=reviewed,
            rankings_sha=snapshot_hash)
            for identifier in organization_ids}
        researchers = comparison_researchers(comparisons, org_names)
    except ValueError as error:
        st.error(str(error))
        return
    if multiple:
        st.markdown("**Portfolio summary**")
        st.caption(" · ".join(f"{org_names[i]}: {comparisons[i]['summary']['members']} current members" for i in organization_ids))
        group_rows = {i: portfolio_summary_rows(c["summary"]) for i, c in comparisons.items()}
        portfolio = [{"Measure":group_rows[i][index]["Measure"], "Group":org_names[i],
            **{k:v for k,v in group_rows[i][index].items() if k != "Measure"}}
            for index in range(len(group_rows[organization_ids[0]])) for i in organization_ids]
    else:
        summary = comparisons[organization_ids[0]]["summary"]
        st.markdown(f"**Portfolio summary · {summary['members']} current members**")
        portfolio = portfolio_summary_rows(summary)
    with st.container(key="portfolio_group_summary" if multiple else "portfolio_summary"):
        st.table(pd.DataFrame(portfolio), hide_index=True, border="horizontal", width="stretch")
    st.caption("Totals count each shared paper once within each group. Fractional totals sum selected members' shares "
        "(1 ÷ number of authors). Per-member figures divide by all selected current members." +
        (" AIS = Article Influence Score." if SHOW_AIS else ""))
    overlap = comparison_overlap(comparisons)
    if multiple:
        shared_people = overlap["researchers_in_multiple_groups"]
        shared_papers = overlap["publications_in_multiple_groups"]
        st.caption(f"{shared_people} {'researcher' if shared_people == 1 else 'researchers'} and "
            f"{shared_papers} {'paper' if shared_papers == 1 else 'papers'} appear in more than one selected group. "
            "Each group is assessed separately; the researcher table lists each person once.")
    for identifier, comparison in comparisons.items():
        summary = comparison["summary"]
        prefix = org_names[identifier] + ": " if multiple else ""
        if not summary["members"] and multiple:
            st.info(prefix + "No current members match these filters.")
        if summary["assessed_members"] < summary["members"]:
            st.warning(prefix + f"Records are available for {summary['assessed_members']}/{summary['members']} selected members. "
                "Totals use available records; per-member figures divide by all selected current members.")
    sources = [r["source_coverage"] for r in researchers if r["source_coverage"] is not None]
    incomplete_primary = sum(not s["primary_complete"] for s in sources)
    if incomplete_primary:
        st.warning(f"Public ORCID works were not fully retrieved for {incomplete_primary} assessed researchers. "
            "Their assessments use available supplementary records; check their source coverage.")
    incomplete_supplements = sum(bool(s["supplementary_issues"]) for s in sources)
    if incomplete_supplements:
        st.info(f"Supplementary sources are unavailable or partial for {incomplete_supplements} researchers. "
            "Their retrieved ORCID works remain included; see Supplements for the source status.")
    st.caption("ORCID is the primary publication source. Crossref and OpenAlex enrich metadata and add verified records. "
        "ORCID counts include all publicly listed work types; journal-paper totals apply the selected field and year filters. "
        "Successful public retrieval does not establish a complete bibliography.")
    doi_less = len({p["work_id"] for c in comparisons.values() for p in c["publications"] if not p["doi"]})
    if doi_less:
        st.info(f"{doi_less} records have no DOI and remain separate until their identities are reviewed. They may include unresolved duplicates across researchers.")
    for identifier, comparison in comparisons.items():
        prefix = org_names[identifier] + " · " if multiple else ""
        st.caption(prefix + "Journal measure: RePEc recursive IF, all-period · ranking snapshot 14 October 2026. " +
            comparison_coverage(comparison["summary"]) +
            " Institutional full count counts each paper once; fractional credit sums the shares of its selected members.")
    if not researchers:
        st.info("No current members match these filters. Change the categories or review memberships.")
        return
    metrics = [metric for metric in COMPARISON_METRICS if SHOW_AIS or "AIS" not in metric]
    sort_by = st.radio("Order researchers by", metrics, horizontal=True)
    score_key = COMPARISON_METRICS[sort_by]["researcher"]
    rows = sorted(researchers, key=lambda r: (r[score_key] is None, -(r[score_key] or 0), r["name"]))
    display = pd.DataFrame([{"Researcher":r["name"], "Papers":r["publications"], "Fractional":r["fractional_publications"],
        "Sum IF":r["recursive_full"], "Fractional IF":r["recursive_fractional"],
        "Sum AIS":r["ais_sum"], "Fractional AIS":r["fractional_ais_sum"],
        **({"Groups":" · ".join(r["groups"])} if multiple else {}),
        "IF coverage":f"{r['recursive_coverage']}/{r['publications']}" if r["publications"] is not None else "—",
        "AIS coverage":f"{r['ais_coverage']}/{r['publications']}" if r["publications"] is not None else "—",
        "ORCID public works":r["source_coverage"]["primary_label"] if r["source_coverage"] else "Not collected",
        "Supplements":r["source_coverage"]["supplementary_status"] if r["source_coverage"] else "Not collected",
        "Needs review":r["review_cases"], "Records as of":r["assessment_as_of"],
        "Membership":" · ".join((org_names[m["organization_id"]] + ": " if multiple else "") +
            STATUS_LABELS[m["membership_status"]] for m in r["group_memberships"])} for r in rows])
    if not SHOW_AIS:
        display = display.drop(columns=["Sum AIS", "Fractional AIS", "AIS coverage"])
    display["Papers"] = display["Papers"].astype("Int64")
    profile_links = ["?" + urlencode({"researcher":r["researcher_id"]}) for r in rows]
    link_names = dict(zip(profile_links, display["Researcher"]))
    linked_display = display.copy()
    linked_display["Researcher"] = profile_links
    # Style only the UI copy so the CSV export keeps plain names.
    st.dataframe(linked_display.style.format({"Researcher":link_names.get}), hide_index=True, width="stretch",
        column_config={"Researcher":st.column_config.LinkColumn("Researcher",
            help="Open this researcher's record in a new tab."),
            "Papers":st.column_config.NumberColumn(format="%d"),
            "Fractional":st.column_config.NumberColumn(format="%.3f", help="Sum of 1 / author count for this researcher's included papers."),
            "Supplements":st.column_config.TextColumn(width=240,
                help="Status of supplementary publication sources. Full details appear in the researcher's record."),
            **{c:st.column_config.NumberColumn(format="%.3f") for c in ("Sum IF","Fractional IF","Sum AIS","Fractional AIS")}})
    annual_per_member = st.radio("Annual output scale", ["Total", "Per member"], horizontal=True,
        key="annual_output_scale", help="Per member divides each group's annual output by all selected current members, using the same membership count for every year.") == "Per member"
    trend, concentration = st.columns(2)
    group_names = [org_names[i] for i in organization_ids]
    colors = {org_names[i]:GROUP_COLORS[index % len(GROUP_COLORS)] for index, i in enumerate(current_organization_ids())}
    with trend:
        annual_label = sort_by + ("/member" if annual_per_member else "")
        st.markdown(f"**Annual output · {annual_label}**")
        annual = annual_group_output(comparisons, sort_by, org_names, per_member=annual_per_member)
        if annual:
            plotted = [name for name in group_names if any(row["group"] == name for row in annual)]
            st.vega_lite_chart(spec=annual_spec(annual, sort_by, groups=plotted, colors=colors,
                per_member=annual_per_member), width="stretch")
        else:
            st.info("No dated publication records are available for an annual series.")
        st.caption(("Annual output divided by each group's selected current members, including members without collected records. "
            "The same denominator applies to every year; shared papers count once within each group. " if annual_per_member else
            "Institution totals by publication year, with shared papers counted once within each group. ") +
            "Fixed journal scores and current membership apply throughout; the latest year may be incomplete.")
    with concentration:
        st.markdown(f"**Concentration · {sort_by}**")
        lorenz_results, points = {}, []
        for identifier, comparison in comparisons.items():
            try:
                lorenz = lorenz_curve(comparison, sort_by)
            except ValueError as error:
                st.warning((org_names[identifier] + ": " if multiple else "") + str(error))
                continue
            lorenz_results[identifier] = lorenz
            points.extend(dict(point, group=org_names[identifier]) for point in lorenz["points"])
        if points:
            plotted = [org_names[i] for i, result in lorenz_results.items() if result["points"]]
            st.vega_lite_chart(spec=lorenz_spec(points, groups=plotted, colors=colors), width="stretch")
        for identifier, lorenz in lorenz_results.items():
            prefix = org_names[identifier] + " · " if multiple else ""
            if lorenz["points"]:
                st.caption(prefix + f"Gini: {lorenz['gini']:.3f} · {lorenz['available']}/{lorenz['selected']} researchers with available scores. "
                    "Researchers run from lowest to highest score. Dashed line: equal shares.")
            elif lorenz["available"]:
                st.info(prefix + "All available scores are zero; Lorenz shares and Gini are undefined.")
            else:
                st.info(prefix + "No researcher scores are available for a Lorenz curve.")
        st.caption("Uses individual researcher scores, including zeros; missing scores are omitted. "
            "Full-count measures credit each coauthor, so their sum can exceed institution totals.")
    col1, col2 = st.columns(2)
    export = dict(version=1, groups=comparisons,
        group_names={i:org_names[i] for i in organization_ids}, overlap=overlap) if multiple else comparisons[organization_ids[0]]
    if PUBLIC_MODE:
        export = public_export(export, include_ais=SHOW_AIS)
    filename = "_".join(organization_ids)
    col1.download_button("Download comparison JSON", json.dumps(export, ensure_ascii=False, indent=2),
        file_name=f"{filename}_comparison.json", mime="application/json")
    csv_display = display.rename(columns={"Fractional":"Fractional publications (1 / author count)",
        "Sum IF":"RePEc recursive IF (all-period) full count", "Fractional IF":"RePEc recursive IF (all-period) fractional",
        "Sum AIS":"JCR AIS full count", "Fractional AIS":"JCR AIS fractional"})
    col2.download_button("Download researcher table CSV", csv_display.to_csv(index=False),
        file_name=f"{filename}_researchers.csv", mime="text/csv")
    with st.expander("Papers contributing to the selected groups" if multiple else "Papers contributing to this institution"):
        if multiple:
            tabs = st.tabs(group_names)
            for tab, identifier in zip(tabs, organization_ids):
                with tab:
                    comparison_papers_table(comparisons[identifier])
        else:
            comparison_papers_table(comparisons[organization_ids[0]])
    with st.expander("How to read this comparison"):
        st.write("Full count adds each paper's journal IF. Fractional count divides it by the number of authors. "
            "Per-member figures divide institution totals by all selected current members, including members without records. "
            "The journal IF uses its full available RePEc publication history; the publication-year controls select the researcher's papers. "
            "All selected publications count toward every current membership, including papers published before joining. "
            "Groups can overlap and their totals should not be added without deduplicating papers. "
            "Editorials, prefaces and special-issue introductions are excluded from publication totals and scores. "
            "Membership in a research center does not by itself establish employment at its host university.")
        st.write("Only assessments using the same journal snapshot, bibliographic year policy and unrestricted collection period "
            "enter this comparison. Public discovery may omit papers even when requests completed successfully. "
            "A comparison export retains the selected membership records and source policies.")
        st.json({org_names[i]:c["policy"] for i,c in comparisons.items()} if multiple else comparisons[organization_ids[0]]["policy"])
    for identifier, comparison in comparisons.items():
        for issue in comparison["issues"]:
            st.warning((org_names[identifier] + ": " if multiple else "") + issue)


def researcher_page():
    st.title("Researcher records")
    researcher_id = person_picker("profile_person")
    if not researcher_id:
        return
    st.query_params["researcher"] = researcher_id
    st.session_state["_profile_route"] = researcher_id
    person = next(p for p in people if p["researcher_id"] == researcher_id)
    st.caption(f"ORCID: {person['orcid'] or 'Not linked yet'} · All collected publication years")
    memberships = [m for m in store.memberships() if m["researcher_id"] == researcher_id]
    st.write("**Affiliations:** " + (" · ".join(f"{m['short_name']} ({STATUS_LABELS[m['status']]})" for m in memberships) or "None recorded"))
    claims = store.query("SELECT organization_name,provider,source_url,retrieved_at,evidence_json FROM affiliation_claims WHERE researcher_id=?", (researcher_id,))
    with st.expander(f"Affiliation source evidence ({len(claims)})"):
        if claims:
            st.dataframe(pd.DataFrame([{**{k:v for k,v in c.items() if k != "evidence_json"},
                "Interpretation":json.loads(c["evidence_json"]).get("interpretation", "")} for c in claims]),
                hide_index=True, width="stretch", column_config={"source_url":st.column_config.LinkColumn("Source")})
            st.json([json.loads(c["evidence_json"]) for c in claims], expanded=False)
        else:
            if PUBLIC_MODE:
                if person["profile_url"]:
                    st.markdown(f"[Official researcher profile]({person['profile_url']})")
                if person["orcid"]:
                    st.markdown(f"[Public ORCID record](https://orcid.org/{person['orcid']})")
                st.caption("Membership status reflects the source directory and the reviews saved in this snapshot.")
            else:
                st.info("Collect this researcher's public records to retrieve ORCID, OpenAlex and available RePEc evidence.")
    assessments = store.latest_assessments(researcher_id=researcher_id, rankings_sha=snapshot_hash,
        year_policy="bibliographic", unrestricted=True)
    if not assessments:
        st.info("This researcher does not yet have an assessment in the published snapshot." if PUBLIC_MODE else
            "No assessment using the current all-period scoring policy is available. Open Collect & import and select this researcher.")
        return
    bundle = json.loads(assessments[0]["bundle_json"])
    summary = bundle["summary"]
    st.markdown("**Portfolio summary**")
    portfolio = pd.DataFrame([
        {"Measure":"Publications", "Total":number(summary["publications"], 0),
            "Fractional total":number(summary["fractional_publications"])},
        {"Measure":"RePEc recursive IF · all-period", "Total":number(summary["repec_recursive_if_all_sum"]),
            "Fractional total":number(summary["fractional_repec_recursive_if_all_sum"])},
        {"Measure":"JCR AIS · 2025", "Total":number(summary["ais_sum"]),
            "Fractional total":number(summary["fractional_ais_sum"])},
    ])
    if not SHOW_AIS:
        portfolio = portfolio.iloc[:2]
    with st.container(key="portfolio_summary"):
        st.table(portfolio, hide_index=True, border="horizontal", width="stretch")
    st.caption("Included journal papers. Fractional totals divide each paper's count or IF by its number of authors." if not SHOW_AIS else
        "Included journal papers. Fractional totals divide each paper's count, IF or AIS by its number of authors. AIS = Article Influence Score.")
    coverage = (f"Records as of {bundle['as_of']}. RePEc IF coverage {summary['repec_recursive_if_all_coverage']}/{summary['publications']}; "
        f"fractional {summary['fractional_repec_recursive_if_all_coverage']}/{summary['publications']}.")
    if SHOW_AIS:
        coverage += (f" AIS coverage {summary['ais_coverage']}/{summary['publications']}; "
            f"fractional {summary['fractional_ais_coverage']}/{summary['publications']}.")
    st.caption(coverage)
    source_coverage = publication_coverage(bundle)
    st.write("**Primary source — ORCID:** " + source_coverage["primary_label"])
    if not source_coverage["primary_complete"]:
        st.warning("The public ORCID works section was not fully retrieved. Available supplementary records are retained.")
    st.caption("Supplementary sources: " + source_coverage["supplementary_status"] + ". "
        "Supplementary failures do not discard ORCID works. Public ORCID retrieval does not establish a complete bibliography.")
    with st.expander("Publication discovery coverage"):
        st.dataframe(pd.DataFrame(coverage_rows(bundle)), hide_index=True, width="stretch")
        for warning in bundle.get("warnings", []):
            st.write(warning)
    if not PUBLIC_MODE:
        journal_grades(summary)
    states = st.multiselect("Show publication decisions", ["included","review","excluded"], default=["included","review"])
    selected_publications = sorted((p for p in bundle["publications"] if p["state"] in states),
        key=lambda p: p["publication_year"] or 0, reverse=True)
    display = pd.DataFrame([{"Year":p["publication_year"], "Journal":p["journal_title"],
        "Authors":p["author_count"], "Recursive IF":p["repec_recursive_if_all"],
        "Fractional IF":p["fractional_repec_recursive_if_all"], "AIS":p["ais"],
        "Fractional AIS":p["ais"] * p["fractional_credit"]
            if p["ais"] is not None and p["fractional_credit"] is not None else None,
        "ABS":p["abs_grade"], "ABDC":p["abdc_grade"], "Publication":p["title"], "Decision":p["state"],
        "Reason":p["reason"].replace("_"," "), "DOI":f"https://doi.org/{p['doi']}" if p["doi"] else None,
        "IF availability":p.get("repec_note") or (
            "Available" if p["repec_recursive_if_all"] is not None else "Journal link requires review")}
        for p in selected_publications])
    if PUBLIC_MODE:
        display = display.drop(columns=["ABS", "ABDC"] + ([] if SHOW_AIS else ["AIS", "Fractional AIS"]), errors="ignore")
    st.dataframe(display, hide_index=True, width="stretch",
        column_config={"Journal":st.column_config.TextColumn(width="medium"),
            "Recursive IF":st.column_config.NumberColumn(help="RePEc recursive impact factor, all-period."),
            "Fractional IF":st.column_config.NumberColumn(help="RePEc recursive IF divided by the number of authors."),
            "AIS":st.column_config.NumberColumn(help="JCR Article Influence Score."),
            "Fractional AIS":st.column_config.NumberColumn(help="AIS divided by the number of authors."),
            "Publication":st.column_config.TextColumn(width="large"), "DOI":st.column_config.LinkColumn("DOI"),
            "IF availability":st.column_config.TextColumn(help="Verified RePEc linkage or the reviewed reason a factor is unavailable in the saved snapshot.", width="large")})
    if not bundle["publications"]:
        st.info("No publication records were retrieved in this assessment. Check the source coverage before interpreting the count.")
    elif not PUBLIC_MODE:
        publication_review(person, bundle)
    st.download_button("Download researcher snapshot" if PUBLIC_MODE else "Download full researcher assessment",
        json.dumps(public_export(bundle, include_ais=SHOW_AIS) if PUBLIC_MODE else bundle, ensure_ascii=False, indent=2),
        file_name=f"{bundle['orcid']}_assessment.json", mime="application/json")


def journal_grades(summary):
    st.write("**Journal grades**")
    st.caption("Number and share of included journal papers at each grade, with the highest grades first. "
        "A paper is counted once in each ranking; missing ratings appear as No rating.")
    grade_columns = st.columns(2)
    for column, label, field, order in zip(grade_columns, ("ABS / AJG", "ABDC"),
            ("abs_distribution", "abdc_distribution"), (["4*", "4", "3", "2", "1"], ["A*", "A", "B", "C"])):
        with column:
            st.write(f"**{label}**")
            total = summary["publications"]
            if not total:
                st.caption("No included journal papers.")
                continue
            values = summary[field]
            grades = order + [grade for grade in values if grade not in order]
            rows = [{"Journal grade": "No rating" if grade == "unavailable" else grade,
                "Papers": values.get(grade, 0), "Share of papers": f"{100 * values.get(grade, 0) / total:.1f}%"}
                for grade in grades]
            rows.append({"Journal grade": "Total", "Papers": total, "Share of papers": "100.0%"})
            st.table(pd.DataFrame(rows), hide_index=True, border="horizontal", width="stretch")


def publication_review(person, bundle):
    with st.expander("Review a publication"):
        pubs = {p["publication_id"]:p for p in bundle["publications"]}
        publication_id = st.selectbox("Publication to review", list(pubs), format_func=lambda value:pubs[value]["title"])
        publication = pubs[publication_id]
        with st.form("publication_review_" + publication_id):
            include = st.checkbox("Include in journal publication output", value=publication["state"] == "included")
            author_count = st.number_input("Author count (0 leaves the reported count unchanged)", min_value=0, value=0, step=1)
            note = st.text_input("Reason for this decision", key="publication_review_note_" + publication_id)
            submitted = st.form_submit_button("Save decision and recalculate")
        if submitted:
            try:
                with st.spinner("Recalculating from cached metadata…"):
                    review_publication(store, person, publication, include=include, author_count=author_count,
                        note=note, as_of=bundle["as_of"])
                st.success("Decision saved. Current-affiliation comparisons use the updated assessment.")
                st.rerun()
            except (ValueError, RuntimeError) as error:
                st.error(str(error))


def memberships_page():
    st.title("Current affiliations")
    st.caption("Changing a current membership moves the researcher's selected publication portfolio to that organization. Historical paper affiliations are not required.")
    organization_id = organization_picker("membership_organization")
    if organization_id:
        members = store.memberships(organization_id)
        st.dataframe(pd.DataFrame([{"Researcher":m["name"], "Category":m["membership_type"], "Status":STATUS_LABELS[m["status"]],
            "Checked":m["checked_on"], "ORCID":m["orcid"], "Profile":m["profile_url"], "Note":m["note"]} for m in members]),
            hide_index=True, width="stretch", column_config={"Profile":st.column_config.LinkColumn("Official profile")})
        person_id = person_picker("membership_person")
        if person_id:
            current = next((m for m in members if m["researcher_id"] == person_id), None)
            with st.form("membership_review_" + organization_id + "_" + person_id):
                category = st.text_input("Membership type", value=current["membership_type"] if current else "Member")
                statuses = list(STATUS_LABELS)
                status = st.selectbox("Current status", statuses,
                    index=statuses.index(current["status"]) if current else 1, format_func=STATUS_LABELS.get)
                note = st.text_input("Review note or source", key="membership_note_" + organization_id + "_" + person_id)
                submitted = st.form_submit_button("Save membership")
            if submitted:
                try:
                    store.set_membership(person_id, organization_id, category, status, note)
                    st.rerun()
                except ValueError as error:
                    st.error(str(error))
    with st.expander("Add another university or research center"):
        with st.form("new_organization"):
            name = st.text_input("Organization name")
            short_name = st.text_input("Short name")
            kind = st.selectbox("Organization type", ["Research center","University","Department","School"])
            url = st.text_input("Official website")
            submitted = st.form_submit_button("Add organization")
        if submitted:
            try:
                store.add_organization(name, short_name, kind, url)
                st.rerun()
            except ValueError as error:
                st.error(str(error))


def collect_page():
    st.title("Collect & import")
    st.caption("Add researchers, collect their publication records, and retain the sources behind affiliation claims.")
    candidates = [p for p in people if p["orcid"]]
    ids = [p["researcher_id"] for p in candidates]
    selected = st.multiselect("Researchers to collect", ids, format_func=person_names.get, max_selections=10)
    cols = st.columns(2)
    offline = cols[0].checkbox("Use cached metadata only", key="collection_offline")
    refresh = cols[1].checkbox("Refresh publication metadata", key="collection_refresh", disabled=offline)
    if st.button("Collect selected researchers", type="primary", disabled=not selected):
        progress = st.progress(0)
        for i, researcher_id in enumerate(selected):
            person = next(p for p in candidates if p["researcher_id"] == researcher_id)
            try:
                with st.spinner(f"Collecting {person['name']}…"):
                    bundle, warnings = run_and_import(store, person["orcid"], offline=offline, refresh=refresh and not offline)
                st.success(f"{person['name']}: {bundle['summary']['publications']} included papers; records saved.")
                for warning in warnings:
                    st.warning(warning)
            except (RuntimeError, ValueError, OSError) as error:
                st.error(f"{person['name']}: {error}")
            progress.progress((i+1)/len(selected))
        st.info("Open Compare or Researchers to inspect the saved results.")
    pending_files = {}
    for path in sorted((ROOT / "data/organizations").glob("*/*/unresolved_orcids.json")):
        pending_files[path.parent.parent.name] = path
    unlinked_profiles = {p["profile_url"] for p in people if not p["orcid"] and p["profile_url"]}
    unlinked_ids = {p["researcher_id"] for p in people if not p["orcid"]}
    source_ids = {(i["scheme"],i["value"]):i["researcher_id"] for i in store.query(
        "SELECT scheme,value,researcher_id FROM person_identifiers WHERE scheme LIKE 'roster:%'")}
    pending_rows = []
    pending_people = set()
    for organization_id,path in pending_files.items():
        for item in json.loads(path.read_text())["profiles"]:
            researcher_id = source_ids.get(("roster:"+organization_id,item.get("source_key")))
            if researcher_id not in unlinked_ids and item.get("profile_url") not in unlinked_profiles:
                continue
            pending_people.add(researcher_id or item.get("profile_url"))
            for candidate in item.get("candidates") or [{}]:
                pending_rows.append({"Organization":org_names.get(organization_id,organization_id),
                    "Researcher": item["name"], "Candidate": candidate.get("name", "No verified candidate"),
                    "ORCID": candidate.get("url", ""), "What needs checking": item["reason"],
                    "Directory / profile": item.get("profile_url") or item.get("source_url")})
    if pending_rows:
        with st.expander(f"Unresolved ORCID identities ({len(pending_people)})"):
            st.caption("These candidates are unlinked. Confirm the person's identity before saving an ORCID in the form below.")
            st.dataframe(pd.DataFrame(pending_rows), hide_index=True, width="stretch", column_config={
                "ORCID": st.column_config.LinkColumn("Candidate ORCID"), "Directory / profile": st.column_config.LinkColumn("Directory / profile")})
    with st.expander("Add a researcher"):
        with st.form("new_researcher"):
            name = st.text_input("Researcher name")
            orcid = st.text_input("ORCID (optional)", key="new_researcher_orcid")
            submitted = st.form_submit_button("Add researcher")
        if submitted:
            try:
                store.add_person(name, orcid or None)
                st.rerun()
            except (ValueError, sqlite3.IntegrityError) as error:
                st.error(str(error))
    with st.expander("Link or correct an ORCID"):
        researcher_id = person_picker("identifier_person")
        if researcher_id:
            with st.form("identifier_review_" + researcher_id):
                orcid = st.text_input("Verified ORCID", key="verified_orcid_" + researcher_id)
                note = st.text_input("Identifier source or review note")
                submitted = st.form_submit_button("Save identifier")
            if submitted:
                try:
                    store.set_orcid(researcher_id, orcid, note)
                    st.rerun()
                except (ValueError, sqlite3.IntegrityError) as error:
                    st.error(str(error))
    with st.expander("Import an existing assessment"):
        uploaded = st.file_uploader("Assessment JSON generated by this program", type="json")
        if uploaded and st.button("Import assessment"):
            try:
                store.import_assessment(json.loads(uploaded.getvalue()), uploaded.name)
                st.success("Assessment imported.")
            except (ValueError, KeyError, sqlite3.IntegrityError) as error:
                st.error(str(error))
    st.caption("ORCID is the primary publication source; Crossref and OpenAlex provide supplementary records and metadata, "
        "with available RePEc affiliation evidence. "
        "Source claims do not automatically change reviewed current memberships. Scopus is not connected in this version.")


def journals_page():
    st.title("Journal reference")
    st.caption("Collected editions: JCR/AIS 2025 · ABS 2024 · ABDC 2025 · RePEc snapshot 14 October 2026")
    query = st.text_input("Search journal title", value="")
    source = st.selectbox("Ranking source", ["repec","jcr","abs","abdc"])
    with sqlite3.connect(DEFAULT_RANKINGS.resolve().as_uri()+"?mode=ro", uri=True) as connection:
        frame = pd.read_sql_query("""SELECT source_title AS Journal,metric_label AS Measure,horizon AS Window,
            numeric_value AS Score,grade_value AS Grade,rank AS Rank,category AS Category,
            edition_label AS Edition,source_url AS Source FROM current_observations
            WHERE source_id=? AND source_title LIKE ? ORDER BY source_title,metric_id,horizon LIMIT 200""",
            connection, params=(source, "%"+query+"%"))
    st.dataframe(frame, hide_index=True, width="stretch", column_config={"Source":st.column_config.LinkColumn("Source")})
    st.caption("Showing up to 200 source observations. RePEc aggregate ranks/scores and individual impact factors use different scales. "
        "ABS grades originate from the documented third-party workbook, with selected official checks.")


def methodology_page():
    st.title("Methodology & sources")
    st.write("This observatory compares retrieved economics and finance journal-publication portfolios. "
        "It is an independent journal-weighted output measure, not an official RePEc author or institution ranking.")
    st.write("Every selected publication follows its researcher to their current affiliations, including papers written before joining. "
        "Within each group, full counts credit each identified paper once. Fractional counts sum its selected members' "
        "shares (1 divided by the total number of authors). Per-member figures divide by all selected current members, "
        "including members whose publications have not been collected. Overlapping group totals are not additive.")
    st.write("Journal weights are RePEc recursive impact factors using all available publication history, "
        "from the fixed snapshot of 14 October 2026. The publication-year filters select papers; they do not change "
        "the journal factor's horizon. The saved Economics/Finance classification and reviewed journal identities "
        "determine which journal articles enter the portfolio.")
    st.write("Editorials, prefaces and special-issue introductions are excluded from publication counts and all derived scores. "
        "The source records and the reasons for exclusion remain available in researcher profiles.")
    st.write("ORCID is the primary publication source. Crossref and OpenAlex supplement and enrich its public records; "
        "verified institutional additions are labelled separately. Public records may be incomplete. "
        "Missing records, journal factors and author counts remain missing, with coverage shown alongside scores. "
        "Researchers without assessments are not treated as having zero output.")
    st.write("The annual chart uses each group's deduplicated papers. Lorenz curves use available individual researcher "
        "scores, including zeros, and normalize each group separately. Full-count coauthorship can therefore make "
        "the sum of individual scores exceed the institution's full-count total. Scores support exploration and "
        "should be read alongside the publications themselves.")
    if SHOW_AIS:
        st.write("AIS means the JCR Article Influence Score, using the fixed 2025 edition. Full AIS sums the "
            "journal's score for each included paper; fractional AIS divides it by the number of authors. "
            "Institution totals use the same deduplication and author shares as RePEc IF. Missing AIS stays missing.")
        st.caption("AIS observations are loaded separately and are absent from the public GitHub snapshot. "
            "Their values and derived scores are visible to this app's viewers. ABS and ABDC grades remain in the local workspace.")
    else:
        st.caption("This public edition publishes counts and RePEc measures. AIS, ABS and ABDC data and their derived "
            "scores remain outside the public snapshot pending confirmation of redistribution permissions.")
    for label, url in (("RePEc journal factors", "https://ideas.repec.org/top/top.journals.recurse.html"),
            ("ORCID", "https://orcid.org/"), ("Crossref", "https://www.crossref.org/"),
            ("OpenAlex", "https://openalex.org/"), ("NIPE", "https://nipe.eeg.uminho.pt/en/people/"),
            ("UECE / REM", "https://www.rem.rc.iseg.ulisboa.pt/")):
        st.markdown(f"[{label}]({url})")
    st.caption(f"Published snapshot: {public_manifest['published_on']}. Individual record dates appear in the researcher table.")


requested_researcher = st.query_params.get("researcher")
if requested_researcher and requested_researcher not in person_names:
    st.warning("The researcher in this link was not found. Select a researcher from the Researchers page.")
elif requested_researcher and requested_researcher != st.session_state.get("_profile_route"):
    st.session_state["workspace_page"] = "Researchers"
    st.session_state["profile_person"] = requested_researcher
    st.session_state["_profile_route"] = requested_researcher

with st.sidebar:
    st.markdown("### Economics & Finance")
    st.caption("RESEARCHER OBSERVATORY")
    pages = ["Compare", "Researchers", "Methodology & sources"] if PUBLIC_MODE else ["Compare","Researchers","Memberships","Collect & import","Journals"]
    if st.session_state.get("workspace_page") not in pages:
        st.session_state["workspace_page"] = "Compare"
    page = st.radio("Workspace", pages,
        key="workspace_page", label_visibility="collapsed", on_change=clear_profile_route)
    st.divider()
    st.caption(f"{len(people)} researcher identities · {len(organizations)} organizations")
    st.caption("Current affiliation attribution\n\n" + ("Hosted read-only snapshot" if PUBLIC_MODE else "Private local workspace"))

{"Compare":comparison_page,"Researchers":researcher_page,"Memberships":memberships_page,
 "Collect & import":collect_page,"Journals":journals_page,"Methodology & sources":methodology_page}[page]()
