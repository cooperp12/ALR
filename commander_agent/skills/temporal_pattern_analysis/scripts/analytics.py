from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
try:
    import polars as pl
except Exception:  # setup-ALR installs Polars; fallback keeps source/tests inspectable
    pl = None


def _as_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return float(default)


def _parse_time(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    try:
        num = float(text)
        return datetime.fromtimestamp(num, tz=timezone.utc)
    except Exception:
        pass
    text = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _value_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return [str(x) for x in value if x not in (None, "")]
    text = str(value).strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(x) for x in parsed if x not in (None, "")]
        except Exception:
            pass
    # Splunk may serialise multivalue fields as newline-delimited text.
    if "\n" in text:
        return [x for x in (p.strip() for p in text.splitlines()) if x]
    return [text]


def _iso_from_epoch(value):
    return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()


def _linear_slope(times, values):
    if len(values) < 2:
        return 0.0
    x = np.asarray(times, dtype=float)
    x = x - x[0]
    y = np.array(values, dtype=float)
    if np.allclose(x, x[0]):
        return 0.0
    return float(np.polyfit(x, y, 1)[0])


def _series_features(times, values):
    y = np.asarray(values, dtype=float)
    if y.size == 0:
        return {}
    median = float(np.median(y))
    mad = float(np.median(np.abs(y - median)))
    diffs = np.diff(y)
    out = {
        "minimum": float(np.min(y)),
        "maximum": float(np.max(y)),
        "mean": float(np.mean(y)),
        "median": median,
        "mad": mad,
        "slope_per_second": _linear_slope(times, y.tolist()),
    }
    max_idx = int(np.argmax(y))
    min_idx = int(np.argmin(y))
    out["maximum_time"] = _iso_from_epoch(times[max_idx])
    out["minimum_time"] = _iso_from_epoch(times[min_idx])
    if diffs.size:
        pos = int(np.argmax(diffs))
        neg = int(np.argmin(diffs))
        out["largest_positive_change"] = float(diffs[pos])
        out["largest_positive_change_time"] = _iso_from_epoch(times[pos + 1])
        out["largest_negative_change"] = float(diffs[neg])
        out["largest_negative_change_time"] = _iso_from_epoch(times[neg + 1])
    if mad > 0:
        robust = np.abs(y - median) / mad
        idx = int(np.argmax(robust))
        out["strongest_robust_deviation"] = float(robust[idx])
        out["strongest_robust_deviation_time"] = _iso_from_epoch(times[idx])
    else:
        out["strongest_robust_deviation"] = 0.0
        out["strongest_robust_deviation_time"] = _iso_from_epoch(times[max_idx])

    # Spectral output is descriptive rather than thresholded. It is only defined
    # when enough points exist to contain a non-DC frequency component.
    if y.size >= 3:
        centred = y - np.mean(y)
        spectrum = np.abs(np.fft.rfft(centred)) ** 2
        if spectrum.size > 1:
            non_dc = spectrum[1:]
            peak_index = int(np.argmax(non_dc)) + 1
            total = float(np.sum(non_dc))
            cadence = np.median(np.diff(np.asarray(times, dtype=float)))
            if cadence > 0 and peak_index > 0:
                freq = np.fft.rfftfreq(y.size, d=float(cadence))[peak_index]
                if freq > 0:
                    out["dominant_period_seconds"] = float(1.0 / freq)
                    out["dominant_period_power_ratio"] = float(spectrum[peak_index] / total) if total > 0 else 0.0
    return out


def analyse_rows(rows: list[dict[str, Any]], request: dict) -> dict:
    group_field = request.get("group_field")
    time_field = request.get("time_field")
    measures = set(request.get("measures") or [])
    comparison_feature = request.get("comparison_feature")

    normalised = []
    for row in rows:
        dt = _parse_time(row.get(time_field))
        if dt is None:
            continue
        group = str(row.get(group_field)) if group_field else "__all__"
        normalised.append({
            "group": group,
            # Store numeric UTC epoch seconds in Polars. This avoids relying on the
            # host Python zoneinfo database when Polars converts timezone-aware
            # datetimes back to Python objects (notably on minimal Windows installs).
            "time": float(dt.timestamp()),
            "event_count": _as_float(row.get("event_count")),
            "distinct_count": _as_float(row.get("distinct_count")),
            "distinct_values": _value_list(row.get("distinct_values")),
        })

    if not normalised:
        return {"ok": False, "reason": "No parseable reduced time-series rows were returned."}

    if pl is not None:
        frame = pl.DataFrame(normalised).sort(["group", "time"])
        groups = frame.get_column("group").unique(maintain_order=True).to_list()
        partitions = {
            group: frame.filter(pl.col("group") == group).sort("time")
            for group in groups
        }
        backend = "polars"
    else:
        groups = []
        partitions = {}
        for row in sorted(normalised, key=lambda r: (r["group"], r["time"])):
            if row["group"] not in partitions:
                groups.append(row["group"])
                partitions[row["group"]] = []
            partitions[row["group"]].append(row)
        backend = "python_fallback"

    group_features = []
    chart_rows = []

    for group in groups:
        part = partitions[group]
        if pl is not None:
            times = part.get_column("time").to_list()
            event_values = part.get_column("event_count").to_list()
            distinct_values = part.get_column("distinct_count").to_list()
            value_lists = part.get_column("distinct_values").to_list()
        else:
            times = [r["time"] for r in part]
            event_values = [r["event_count"] for r in part]
            distinct_values = [r["distinct_count"] for r in part]
            value_lists = [r["distinct_values"] for r in part]

        cumulative = None
        new_counts = None
        feature = {
            "group": group,
            "points": len(times),
            "first_time": _iso_from_epoch(times[0]),
            "last_time": _iso_from_epoch(times[-1]),
            "duration_seconds": float(times[-1] - times[0]),
        }

        if "event_count" in measures:
            feature["total_events"] = float(np.sum(event_values))
            feature["event_count"] = _series_features(times, event_values)

        if "distinct_count" in measures:
            feature["max_distinct_count"] = float(np.max(distinct_values)) if distinct_values else 0.0
            feature["distinct_count"] = _series_features(times, distinct_values)

        if "cumulative_distinct_count" in measures or "new_distinct_count" in measures:
            seen = set()
            cumulative = []
            new_counts = []
            for values in value_lists:
                bucket = set(values or [])
                new = bucket - seen
                seen.update(bucket)
                new_counts.append(float(len(new)))
                cumulative.append(float(len(seen)))
            if "cumulative_distinct_count" in measures:
                feature["final_cumulative_distinct"] = cumulative[-1] if cumulative else 0.0
                feature["cumulative_distinct_count"] = _series_features(times, cumulative)
            if "new_distinct_count" in measures:
                feature["max_new_distinct"] = float(np.max(new_counts)) if new_counts else 0.0
                feature["new_distinct_count"] = _series_features(times, new_counts)

        if "event_count" in measures and "distinct_count" in measures and len(times) >= 2:
            a = np.asarray(event_values, dtype=float)
            b = np.asarray(distinct_values, dtype=float)
            if np.std(a) > 0 and np.std(b) > 0:
                feature["event_distinct_correlation"] = float(np.corrcoef(a, b)[0, 1])

        group_features.append(feature)
        for i, dt in enumerate(times):
            chart_rows.append({
                "group": group,
                "time": _iso_from_epoch(dt),
                "event_count": float(event_values[i]),
                "distinct_count": float(distinct_values[i]),
                "cumulative_distinct_count": float(cumulative[i]) if cumulative is not None else 0.0,
                "new_distinct_count": float(new_counts[i]) if new_counts is not None else 0.0,
            })

    comparison = None
    if comparison_feature:
        scored = []
        for feature in group_features:
            value = feature.get(comparison_feature)
            if isinstance(value, (int, float)) and math.isfinite(float(value)):
                scored.append((float(value), feature["group"]))
        if scored:
            best_value = max(v for v, _ in scored)
            comparison = {
                "feature": comparison_feature,
                "maximum": best_value,
                "leaders": sorted(g for v, g in scored if v == best_value),
            }

    return {
        "ok": True,
        "library": {"dataframe": backend, "numeric": "numpy", "preferred_dataframe": "polars"},
        "request": request,
        "row_count": len(normalised),
        "group_count": len(group_features),
        "groups": group_features,
        "comparison": comparison,
        "chart_rows": chart_rows,
    }


def write_graphs(analysis: dict, output_dir: Path, stem: str) -> list[str]:
    if not analysis.get("ok"):
        return []
    try:
        import matplotlib.pyplot as plt
    except Exception:
        return []

    output_dir.mkdir(parents=True, exist_ok=True)
    rows = analysis.get("chart_rows") or []
    if not rows:
        return []
    groups = {}
    for row in sorted(rows, key=lambda r: (r["group"], r["time"])):
        groups.setdefault(row["group"], []).append(row)
    requested = set((analysis.get("request") or {}).get("measures") or [])
    graph_measures = []
    if "event_count" in requested:
        graph_measures.append(("event_count", "Events"))
    if "distinct_count" in requested:
        graph_measures.append(("distinct_count", "Distinct values"))
    if "cumulative_distinct_count" in requested:
        graph_measures.append(("cumulative_distinct_count", "Cumulative distinct values"))
    if "new_distinct_count" in requested:
        graph_measures.append(("new_distinct_count", "New distinct values"))
    paths = []
    for measure, ylabel in graph_measures:
        fig, ax = plt.subplots()
        for group, group_rows in groups.items():
            plot_times = [_parse_time(r["time"]) for r in group_rows]
            ax.plot(plot_times, [r[measure] for r in group_rows], label=str(group))
        ax.set_xlabel("Time")
        ax.set_ylabel(ylabel)
        ax.set_title(f"Temporal analysis: {measure}")
        if len(groups) > 1:
            ax.legend(fontsize="small")
        fig.autofmt_xdate()
        path = output_dir / f"{stem}_{measure}.png"
        fig.savefig(path, bbox_inches="tight")
        plt.close(fig)
        paths.append(str(path))
    return paths
