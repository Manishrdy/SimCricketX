"""Public format metadata shared by routes, templates and engine adapters."""

FORMAT_CATALOG = {
    "T20": {"label": "T20", "family": "limited_overs", "lengths": [20], "rating_source": "T20", "manual": True, "impact_player": True},
    "T10": {"label": "T10", "family": "limited_overs", "lengths": [10], "rating_source": "T20", "manual": True, "impact_player": False},
    "ListA": {"label": "List A", "family": "limited_overs", "lengths": [40, 50], "rating_source": "ListA", "manual": True, "impact_player": False},
    "FC": {"label": "First-Class", "family": "multi_day", "lengths": [], "rating_source": "FC", "manual": False, "impact_player": False},
}
SUPPORTED_FORMATS = tuple(FORMAT_CATALOG)
FORMAT_LABELS = {key: spec["label"] for key, spec in FORMAT_CATALOG.items()}
TOUR_FORMATS = tuple(sorted(SUPPORTED_FORMATS, key=lambda key: max(FORMAT_CATALOG[key]["lengths"] or [1000]), reverse=True))


def default_scheduled_overs(name):
    return max(FORMAT_CATALOG.get(name, FORMAT_CATALOG["T20"])["lengths"] or [None])
