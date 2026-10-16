"""Annual institution output and concentration of available researcher scores."""
from collections import defaultdict
from math import fsum, isfinite


# Insertion order is also the comparison page's requested sorting order.
COMPARISON_METRICS = {
    "Fractional IF": {"researcher":"recursive_fractional", "publication":"fractional_recursive_if"},
    "Sum IF": {"researcher":"recursive_full", "publication":"recursive_if"},
    "Fractional AIS": {"researcher":"fractional_ais_sum", "publication":"fractional_ais"},
    "Sum AIS": {"researcher":"ais_sum", "publication":"ais"},
    "Publications": {"researcher":"publications", "publication":None},
}

GROUP_COLORS = ("#17666E", "#C47632", "#7761A7", "#4276B1", "#55845B", "#B45870")


def annual_group_output(comparisons, metric, names):
    """Align group series to a common year span, retaining missing scores."""
    years = [p["publication_year"] for c in comparisons.values() for p in c["publications"]
             if p["publication_year"] is not None]
    policies = [c["policy"] for c in comparisons.values()]
    bounds = {(p["from_year"], p["to_year"]) for p in policies}
    if len(bounds) > 1:
        raise ValueError("Annual group comparisons must use the same publication-year filters.")
    first, last = next(iter(bounds), (None, None))
    first = first if first is not None else min(years, default=None)
    last = last if last is not None else max(years, default=None)
    rows = []
    for group_id, comparison in comparisons.items():
        aligned = dict(comparison, policy=dict(comparison["policy"], from_year=first, to_year=last))
        rows.extend(dict(row, group=names[group_id]) for row in annual_output(aligned, metric))
    return rows


def group_color_encoding(groups, colors):
    return {"field":"group", "type":"nominal", "title":"Group",
        "scale":{"domain":groups, "range":[colors[group] for group in groups]},
        "legend":{"orient":"bottom", "title":"", "symbolType":"stroke"}}


def annual_output(comparison, metric):
    """Use canonical institution papers; retain empty years and unknown scores."""
    field = COMPARISON_METRICS[metric]["publication"]
    if not comparison["summary"]["assessed_members"]:
        return []
    grouped = defaultdict(list)
    for paper in comparison["publications"]:
        if paper["publication_year"] is not None:
            grouped[paper["publication_year"]].append(paper)
    first = comparison["policy"]["from_year"]
    last = comparison["policy"]["to_year"]
    first = first if first is not None else min(grouped, default=None)
    last = last if last is not None else max(grouped, default=None)
    if first is None or last is None or first > last:
        return []
    rows, segment = [], 0
    for year in range(first, last + 1):
        papers = grouped[year]
        values = [1 if field is None else paper[field] for paper in papers]
        known = [value for value in values if value is not None]
        value = fsum(known) if known else (None if papers else 0.0)
        # Separate paths prevent a line from connecting across unknown years.
        if value is None:
            segment += 1
        rows.append(dict(year=year, value=value, papers=len(papers),
            scored_papers=len(known), segment=segment))
    return rows


def lorenz_curve(comparison, metric):
    """Sort all available individual scores, retaining zeros and excluding nulls."""
    field = COMPARISON_METRICS[metric]["researcher"]
    scores = [row[field] for row in comparison["researchers"] if row[field] is not None]
    if any(not isfinite(score) or score < 0 for score in scores):
        raise ValueError("A Lorenz curve requires finite, nonnegative researcher scores.")
    scores.sort()
    total = fsum(scores) if scores else None
    result = dict(points=[], available=len(scores), selected=len(comparison["researchers"]),
        total=total, gini=None)
    if not total:
        return result
    points = [dict(researcher_share=0.0, output_share=0.0, researchers=0)]
    cumulative = 0.0
    for rank, score in enumerate(scores, 1):
        cumulative = fsum((cumulative, score))
        points.append(dict(researcher_share=rank / len(scores),
            output_share=cumulative / total if rank < len(scores) else 1.0,
            researchers=rank))
    area = fsum((left["output_share"] + right["output_share"]) / (2 * len(scores))
        for left, right in zip(points, points[1:]))
    result.update(points=points, gini=max(0.0, min(1.0, 1 - 2 * area)))
    return result


def annual_spec(rows, metric, *, groups=None, colors=None):
    spec = {
        "height":260, "data":{"values":rows},
        "mark":{"type":"line", "point":{"filled":True,"size":35}, "color":"#17666E"},
        "encoding":{
            "x":{"field":"year", "type":"quantitative", "title":"Publication year",
                "scale":{"zero":False}, "axis":{"format":"d", "tickMinStep":1, "tickCount":5}},
            "y":{"field":"value", "type":"quantitative", "title":metric, "scale":{"zero":True}},
            "detail":{"field":"segment", "type":"nominal"},
            "order":{"field":"year", "type":"quantitative"},
            "tooltip":[{"field":"year", "type":"quantitative", "title":"Year", "format":"d"},
                {"field":"value", "type":"quantitative", "title":metric,
                    "format":",.0f" if metric == "Publications" else ",.3f"},
                {"field":"scored_papers", "type":"quantitative", "title":"Papers with this metric"},
                {"field":"papers", "type":"quantitative", "title":"Included papers"}],
        }, "config":{"view":{"stroke":None}},
    }
    if groups:
        colors = colors or {group:GROUP_COLORS[i % len(GROUP_COLORS)] for i,group in enumerate(groups)}
        spec["mark"].pop("color")
        spec["encoding"]["color"] = group_color_encoding(groups, colors)
        spec["encoding"]["detail"] = [{"field":"group", "type":"nominal"},
                                      {"field":"segment", "type":"nominal"}]
        spec["encoding"]["tooltip"].insert(0, {"field":"group", "type":"nominal", "title":"Group"})
    return spec


def lorenz_spec(points, *, groups=None, colors=None):
    spec = {
        "height":260,
        "encoding":{
            "x":{"field":"researcher_share", "type":"quantitative", "title":"Cumulative researchers",
                "scale":{"domain":[0,1]}, "axis":{"format":".0%", "tickCount":5}},
            "y":{"field":"output_share", "type":"quantitative", "title":"Cumulative output",
                "scale":{"domain":[0,1]}, "axis":{"format":".0%", "tickCount":5}},
        },
        "layer":[
            {"data":{"values":[{"researcher_share":0,"output_share":0},
                {"researcher_share":1,"output_share":1}]},
                "mark":{"type":"line", "color":"#98A5A8", "strokeDash":[5,4]}},
            {"data":{"values":points},
                "mark":{"type":"line", "color":"#17666E", "strokeWidth":2.5},
                "encoding":{"order":{"field":"researcher_share", "type":"quantitative"},
                    "tooltip":[{"field":"researchers", "type":"quantitative", "title":"Lowest-scoring researchers"},
                        {"field":"researcher_share", "type":"quantitative", "title":"Share of researchers", "format":".1%"},
                        {"field":"output_share", "type":"quantitative", "title":"Share of output", "format":".1%"}]}},
        ], "config":{"view":{"stroke":None}},
    }
    if groups:
        colors = colors or {group:GROUP_COLORS[i % len(GROUP_COLORS)] for i,group in enumerate(groups)}
        curve = spec["layer"][1]
        curve["mark"].pop("color")
        curve["encoding"]["color"] = group_color_encoding(groups, colors)
        curve["encoding"]["detail"] = {"field":"group", "type":"nominal"}
        curve["encoding"]["tooltip"].insert(0, {"field":"group", "type":"nominal", "title":"Group"})
    return spec
