# Economics & Finance Research Observatory

Read-only comparison of economics and finance publication portfolios. Built from
public ORCID works, supplementary bibliographic metadata and reviewed institutional
rosters. RePEc recursive journal impact factors use the all-period snapshot dated
14 October 2026. This is an independent journal-weighted output measure, not an
official RePEc author or institution ranking.

Every selected publication follows its researcher's current memberships. Full
institution totals count a shared paper once; fractional credit sums members'
1 / author count shares. Missing records and missing factors remain visible.
Scores are not a substitute for evaluating the content and quality of research.

The published snapshot excludes AIS, ABS and ABDC observations and derived scores,
raw provider responses, maintenance notes, credentials and collection history.
Maintenance takes place separately; this service cannot change the catalog.

The hosted app can load a snapshot-bound AIS supplement from the
ECON_RANKINGS_PRIVATE_AIS Streamlit secret. It restores full and fractional AIS in
summaries, publication tables, comparisons, charts and downloads; the committed
database stays unchanged. AIS values and derived scores are visible to the app's
viewers. Access is controlled through Streamlit sharing settings. Never commit
the supplement or a secrets.toml file.
ABS and ABDC grades remain outside the hosted edition.

## Run

Install requirements.txt, then run `streamlit run public_app.py`.
For Streamlit Community Cloud select main, public_app.py and Python 3.12.

Snapshot metadata: data/manifest.json. The data file is regenerated offline when
reviewed source records change. The full maintenance database is never deployed.

Sources: https://orcid.org/ | https://www.crossref.org/ | https://openalex.org/ |
https://ideas.repec.org/top/top.journals.recurse.html |
https://nipe.eeg.uminho.pt/en/people/ | https://www.rem.rc.iseg.ulisboa.pt/
