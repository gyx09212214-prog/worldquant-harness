"""Build static public visuals for the worldquant-harness harness demo.

The visual pack is intentionally docs-first and dependency-free. It reads local
public harness artifacts, writes sanitized SVG diagrams, and creates a Markdown
guide that maps each visual back to the artifact contract.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from collections import Counter
from html import escape
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

VISUAL_FILENAMES = {
    "overview": "worldquant-harness-overview.svg",
    "trace": "public-demo-trace.svg",
    "memory": "memory-feedback-graph.svg",
    "factor_map": "factor-map-snapshot.svg",
    "quality": "quality-review-dashboard.svg",
    "profile": "profile-evolution-timeline.svg",
}

STATIC_VISUAL_FILENAMES = {
    "lifecycle": "harness-artifact-lifecycle.svg",
    "submit_boundary": "submit-boundary.svg",
    "release_boundary": "release-safety-boundary.svg",
}

PRIVATE_PATH_PATTERNS = (
    re.compile(r"(?:^|[\s\"'`(])([A-Za-z]:\\)"),
    re.compile(r"(?:^|[\s\"'`(])([A-Za-z]:/)"),
    re.compile(r"\\Users\\", re.IGNORECASE),
    re.compile(r"/Users/", re.IGNORECASE),
    re.compile(r"Obsidian Vault", re.IGNORECASE),
)

COLOR = {
    "ink": "#17202a",
    "muted": "#5c6670",
    "line": "#d8dde3",
    "panel": "#f7f9fb",
    "blue": "#2f6fed",
    "blue_soft": "#eaf1ff",
    "green": "#1f9d55",
    "green_soft": "#e8f6ee",
    "red": "#d64545",
    "red_soft": "#fdecec",
    "amber": "#c47f17",
    "amber_soft": "#fff4df",
    "gray": "#eef1f4",
    "white": "#ffffff",
}


def build_public_visual_pack(
    source: str | Path = "reports/public_harness_demo",
    output_dir: str | Path = "docs/images",
    report: str | Path = "docs/VISUAL_GUIDE.md",
) -> dict[str, Any]:
    """Create the public visual pack and return a compact manifest."""

    source_path = Path(source)
    output_path = Path(output_dir)
    report_path = Path(report)
    output_path.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    data = load_visual_data(source_path)
    svgs = {
        "overview": render_overview_svg(data),
        "trace": render_trace_svg(data),
        "memory": render_memory_svg(data),
        "factor_map": render_factor_map_svg(data),
        "quality": render_quality_svg(data),
        "profile": render_profile_svg(data),
    }

    outputs: dict[str, str] = {}
    for key, filename in VISUAL_FILENAMES.items():
        path = output_path / filename
        content = svgs[key]
        _assert_public_content(content, path)
        path.write_text(content, encoding="utf-8")
        outputs[key] = _display_path(path)

    for key, filename in STATIC_VISUAL_FILENAMES.items():
        path = output_path / filename
        source_path = ROOT / "docs" / "images" / filename
        content = ""
        if path.is_file():
            content = path.read_text(encoding="utf-8", errors="replace")
        elif source_path.is_file():
            content = source_path.read_text(encoding="utf-8", errors="replace")
            path.write_text(content, encoding="utf-8")
        if content:
            _assert_public_content(content, path)
            outputs[key] = _display_path(path)

    guide = render_visual_guide(data, output_path, report_path)
    _assert_public_content(guide, report_path)
    report_path.write_text(guide, encoding="utf-8")
    outputs["guide"] = _display_path(report_path)

    return {
        "ok": True,
        "source": _display_path(source_path),
        "outputs": outputs,
        "data": {
            "counts": data["counts"],
            "harness_score": data["harness_score"],
            "quality_available": bool(data["quality"]["available"]),
            "profile_available": bool(data["profile"]["available"]),
        },
    }


def load_visual_data(source: Path) -> dict[str, Any]:
    """Load and normalize visual data from public demo-style artifacts."""

    source = Path(source)
    demo_summary = _read_json(source / "demo_summary.json")
    experiment_dir = _discover_experiment_dir(source, demo_summary)
    files = _known_files(source, demo_summary, experiment_dir)

    candidates = _read_jsonl(files.get("candidate_specs"))
    ready = _read_jsonl(files.get("ready"))
    rejected = _read_jsonl(files.get("rejected"))
    eval_summary = _read_json(files.get("eval_summary"))
    evolution = _read_json(files.get("evolution_result"))
    efficiency = _read_json(_first_existing(source / "efficiency_summary.json", source / "root_efficiency_summary.json"))
    quality_summary = _read_json(_first_existing(source / "quality_review" / "summary.json", *_quality_summary_candidates(source)))
    quality_directions = _read_json(
        _first_existing(source / "quality_review" / "recommended_directions.json", *_quality_direction_candidates(source))
    )

    metrics = _as_dict(eval_summary.get("metrics")) or _as_dict((demo_summary.get("harness") or {}).get("metrics"))
    efficiency_current = _as_dict(efficiency.get("current"))
    funnel = _as_dict(efficiency_current.get("funnel"))
    counts = {
        "candidates": _int(funnel.get("candidates"), len(candidates)),
        "simulated": _int(funnel.get("simulated"), metrics.get("total_simulations"), len(ready) + len(rejected)),
        "reviewed": _int(funnel.get("reviewed"), metrics.get("review_count"), len(ready) + len(rejected)),
        "ready": _int(funnel.get("ready"), metrics.get("ready_count"), len(ready)),
        "rejected": _int(funnel.get("rejected"), metrics.get("presubmit_rejected_count"), len(rejected)),
        "submitted": _int(funnel.get("submitted"), metrics.get("real_submit_attempt_count"), 0),
        "active": _int(funnel.get("active"), metrics.get("real_submit_success_count"), 0),
    }
    reject_counts = _as_dict(eval_summary.get("reject_counts")) or _as_dict((demo_summary.get("harness") or {}).get("reject_counts"))
    field_rows = _field_rows(eval_summary, efficiency_current, candidates, ready, rejected)
    source_rows = _leaderboard_rows(efficiency_current, "source_family")
    directions = _as_list(quality_directions.get("directions"))
    profile = _profile_summary(evolution)
    quality = _quality_summary(quality_summary, directions)

    return {
        "source": source,
        "experiment_id": str(demo_summary.get("experiment_id") or eval_summary.get("experiment_id") or "public demo"),
        "submit_guard": str(demo_summary.get("submit_guard") or "No real WQ submit call is made in the public demo."),
        "real_submit_attempted": bool(demo_summary.get("real_submit_attempted") or False),
        "counts": counts,
        "harness_score": _float(eval_summary.get("harness_score"), (demo_summary.get("harness") or {}).get("score")),
        "metrics": metrics,
        "reject_counts": reject_counts,
        "field_rows": field_rows,
        "source_rows": source_rows,
        "quality": quality,
        "profile": profile,
        "artifacts": {
            key: _artifact_status(path)
            for key, path in files.items()
            if key
        },
    }


def render_overview_svg(data: dict[str, Any]) -> str:
    score = _fmt_score(data.get("harness_score"))
    counts = data["counts"]
    nodes = [
        ("Human Goal", "Research target and constraints", COLOR["blue_soft"], COLOR["blue"]),
        ("Agent", "Generate alpha candidates", COLOR["blue_soft"], COLOR["blue"]),
        ("Harness", "Gate legality, similarity, self-correlation", COLOR["amber_soft"], COLOR["amber"]),
        ("Memory", "Store failures, blockers, candidate_uid trace", COLOR["blue_soft"], COLOR["blue"]),
        ("Profile", "Evolve next search biases", COLOR["green_soft"], COLOR["green"]),
        ("Review", "Explicit human submit boundary", COLOR["red_soft"], COLOR["red"]),
    ]
    parts = [_svg_header(1120, 520, "worldquant-harness agent research harness overview")]
    parts.append(_title(48, 54, "worldquant-harness: agent research harness with memory feedback"))
    parts.append(_subtitle(48, 82, "Sandbox, presubmit, harness score, memory, and profile evolution are visible before any real submit path."))
    x0, y0, gap = 48, 138, 24
    box_w, box_h = 154, 112
    for i, (name, body, fill, stroke) in enumerate(nodes):
        x = x0 + i * (box_w + gap)
        parts.append(_node(x, y0, box_w, box_h, name, body, fill, stroke))
        if i < len(nodes) - 1:
            parts.append(_arrow(x + box_w + 4, y0 + box_h / 2, x + box_w + gap - 4, y0 + box_h / 2, stroke=COLOR["muted"]))
    parts.append(_metric_card(80, 318, "Harness score", score, "workflow quality, not trading PnL", COLOR["green"]))
    parts.append(_metric_card(318, 318, "Candidate funnel", f"{counts['candidates']} -> {counts['ready']} ready", "generated to ready candidates", COLOR["blue"]))
    parts.append(_metric_card(556, 318, "Self-correlation", f"{_int(data['metrics'].get('self_correlation_reject_count'), 0)} blocked", "main pressure signal", COLOR["amber"]))
    parts.append(_metric_card(794, 318, "Submit guard", "no real submit", "public demo records review only", COLOR["red"]))
    parts.append(_footer(48, 486, "What this proves: worldquant-harness is a reproducible research harness loop, not a one-shot alpha script."))
    parts.append("</svg>\n")
    return "".join(parts)


def render_trace_svg(data: dict[str, Any]) -> str:
    counts = data["counts"]
    stages = [
        ("Candidates", counts["candidates"], COLOR["blue"]),
        ("Simulated", counts["simulated"], COLOR["blue"]),
        ("Reviewed", counts["reviewed"], COLOR["amber"]),
        ("Ready", counts["ready"], COLOR["green"]),
        ("Rejected", counts["rejected"], COLOR["red"]),
        ("Submitted", counts["submitted"], COLOR["muted"]),
        ("Active", counts["active"], COLOR["muted"]),
    ]
    max_count = max([value for _, value, _ in stages] + [1])
    parts = [_svg_header(1120, 560, "worldquant-harness public demo trace funnel")]
    parts.append(_title(48, 54, "Public demo trace: candidate_uid lifecycle funnel"))
    parts.append(_subtitle(48, 82, "Each candidate keeps a stable candidate_uid from creation through simulation, review, rejection, ready state, and explicit submit boundaries."))
    y = 132
    for name, value, color in stages:
        parts.append(f'<text x="60" y="{y + 22}" class="label">{escape(name)}</text>')
        width = 720 * (value / max_count)
        parts.append(f'<rect x="190" y="{y}" width="760" height="34" rx="8" fill="{COLOR["gray"]}"/>')
        parts.append(f'<rect x="190" y="{y}" width="{max(width, 4):.1f}" height="34" rx="8" fill="{color}"/>')
        parts.append(f'<text x="970" y="{y + 23}" class="metric">{value}</text>')
        y += 54
    reject_counts = data.get("reject_counts") or {}
    reason_text = ", ".join(f"{key}: {value}" for key, value in sorted(reject_counts.items())[:4]) or "not available in this demo"
    parts.append(_callout(60, 470, 1000, 48, "Reject reasons", reason_text))
    parts.append(_footer(48, 536, "What this proves: the harness turns agent output into an auditable lifecycle, with no hidden submit step."))
    parts.append("</svg>\n")
    return "".join(parts)


def render_memory_svg(data: dict[str, Any]) -> str:
    parts = [_svg_header(1120, 580, "worldquant-harness memory feedback graph")]
    parts.append(_title(48, 54, "Memory feedback graph"))
    parts.append(_subtitle(48, 82, "Memory is shown as evidence flowing into rules and profile changes, then back into the next synthesis direction."))
    nodes = {
        "events": (72, 150, "Lifecycle Events", "candidate_uid, ready, rejected"),
        "blockers": (72, 310, "Blockers", "self-correlation, duplicate, illegal input"),
        "memory": (406, 230, "Memory Layer", "experience rows, rules, failure memory"),
        "quality": (720, 150, "Quality Review", "period score and pressure table"),
        "profile": (720, 310, "Profile Evolution", "biases, limits, repair policy"),
        "next": (406, 440, "Next Directions", _first_direction(data)),
    }
    for key in ("events", "blockers", "memory", "quality", "profile", "next"):
        x, y, title, body = nodes[key]
        fill = COLOR["blue_soft"] if key in {"memory", "profile"} else COLOR["panel"]
        stroke = COLOR["blue"] if key in {"memory", "profile"} else COLOR["line"]
        parts.append(_node(x, y, 250, 92, title, body, fill, stroke))
    parts.extend(
        [
            _arrow(322, 196, 406, 256),
            _arrow(322, 356, 406, 280),
            _arrow(656, 256, 720, 196),
            _arrow(656, 280, 720, 356),
            _arrow(845, 402, 590, 440),
            _arrow(406, 486, 197, 402),
        ]
    )
    sc_blocked = _int(data["metrics"].get("self_correlation_reject_count"), 0)
    parts.append(_metric_card(406, 112, "Main memory signal", f"{sc_blocked} self-correlation block(s)", "pressure is carried into next profile", COLOR["amber"]))
    parts.append(_footer(48, 546, "What this proves: generated alpha quality is not lost; blockers become structured memory for the next run."))
    parts.append("</svg>\n")
    return "".join(parts)


def render_factor_map_svg(data: dict[str, Any]) -> str:
    rows = data.get("field_rows") or []
    rows = rows[:6]
    parts = [_svg_header(1120, 560, "worldquant-harness factor map snapshot")]
    parts.append(_title(48, 54, "Factor map snapshot"))
    parts.append(_subtitle(48, 82, "Field signatures and source families expose crowded areas, ready lanes, and self-correlation pressure."))
    if not rows:
        parts.append(_empty_panel(90, 150, 940, 250, "Field map not available in this demo"))
    else:
        cx0, cy0 = 172, 180
        positions = [(cx0 + (i % 3) * 300, cy0 + (i // 3) * 145) for i in range(len(rows))]
        for i, row in enumerate(rows):
            x, y = positions[i]
            label = str(row.get("field_signature") or row.get("source_family") or row.get("group_key") or "unknown")
            ready = _int(row.get("ready_count"), 0)
            rejected = _int(row.get("rejected_count"), 0)
            fail_share = _float(row.get("self_correlation_fail_share"))
            color = COLOR["green"] if ready and not rejected else COLOR["red"] if rejected else COLOR["amber"]
            radius = 42 + min(_int(row.get("count"), 1) * 5, 22)
            parts.append(f'<circle cx="{x}" cy="{y}" r="{radius}" fill="{COLOR["white"]}" stroke="{color}" stroke-width="4"/>')
            parts.append(f'<text x="{x}" y="{y - 8}" text-anchor="middle" class="label">{escape(_short(label, 24))}</text>')
            parts.append(f'<text x="{x}" y="{y + 17}" text-anchor="middle" class="tiny">ready {ready} / rejected {rejected}</text>')
            if fail_share is not None:
                parts.append(f'<text x="{x}" y="{y + radius + 24}" text-anchor="middle" class="tiny">self-corr fail {fail_share:.0%}</text>')
        parts.append(_legend(780, 430, [("ready lane", COLOR["green"]), ("blocked/crowded", COLOR["red"]), ("watch", COLOR["amber"])]))
    parts.append(_footer(48, 526, "What this proves: the harness can explain why the next alpha direction should shift fields or combine domains."))
    parts.append("</svg>\n")
    return "".join(parts)


def render_quality_svg(data: dict[str, Any]) -> str:
    quality = data["quality"]
    parts = [_svg_header(1120, 560, "worldquant-harness quality review dashboard")]
    parts.append(_title(48, 54, "Period quality review dashboard"))
    parts.append(_subtitle(48, 82, "Submitted and generated alpha quality are reviewed together, with self-correlation pressure made explicit."))
    if not quality["available"]:
        parts.append(_empty_panel(90, 150, 940, 250, "Quality review not available in this demo"))
    else:
        metrics = quality["metrics"]
        cards = [
            ("Period quality", _fmt_score(metrics.get("period_quality_score")), "combined review score", COLOR["blue"]),
            ("Generated pass", _fmt_pct(metrics.get("generated_metric_pass_rate")), "unsubmitted quality", COLOR["amber"]),
            ("Ready rate", _fmt_pct(metrics.get("generated_ready_rate")), "candidate generation yield", COLOR["green"]),
            ("Self-corr fail", _fmt_pct(metrics.get("generated_self_correlation_fail_share")), "main blocker pressure", COLOR["red"]),
        ]
        for i, (title_text, value, desc, color) in enumerate(cards):
            parts.append(_metric_card(70 + i * 255, 132, title_text, value, desc, color))
        buckets = _as_dict(metrics.get("quality_bucket_counts"))
        if buckets:
            parts.append(_mini_bar_chart(90, 300, 430, 170, buckets, "Quality buckets"))
        pressure = quality.get("pressure") or []
        if pressure:
            rows = {str(row.get("group_key") or "unknown"): _float(row.get("self_correlation_fail_share"), 0.0) or 0.0 for row in pressure[:5]}
            parts.append(_mini_bar_chart(600, 300, 430, 170, rows, "Self-correlation pressure"))
    parts.append(_footer(48, 526, "What this proves: the project can review a time window of alpha generation quality, not just one final submission."))
    parts.append("</svg>\n")
    return "".join(parts)


def render_profile_svg(data: dict[str, Any]) -> str:
    profile = data["profile"]
    parts = [_svg_header(1120, 560, "worldquant-harness profile evolution timeline")]
    parts.append(_title(48, 54, "Profile evolution timeline"))
    parts.append(_subtitle(48, 82, "Harness metrics trigger explicit profile candidates before the next experiment is seeded."))
    if not profile["available"]:
        parts.append(_empty_panel(90, 150, 940, 250, "Profile evolution not available in this demo"))
    else:
        steps = [
            ("Harness Eval", f"score {_fmt_score(profile.get('baseline_score'))}", COLOR["blue"]),
            ("Profile Candidate", str(profile.get("recommended_candidate") or "candidate"), COLOR["amber"]),
            ("Policy Changes", "; ".join(profile.get("actions")[:2]) or "tracked changes", COLOR["green"]),
            ("Child Experiment", str(profile.get("child_experiment") or "seeded"), COLOR["blue"]),
        ]
        x = 78
        y = 205
        for i, (title_text, body, color) in enumerate(steps):
            parts.append(_node(x, y, 220, 112, title_text, body, COLOR["white"], color))
            if i < len(steps) - 1:
                parts.append(_arrow(x + 224, y + 56, x + 282, y + 56, stroke=COLOR["muted"]))
            x += 280
        biases = profile.get("priority_biases") or []
        parts.append(_callout(78, 388, 960, 58, "Next profile biases", ", ".join(biases[:5]) or "not available in this demo"))
    parts.append(_footer(48, 526, "What this proves: agent search preferences are versioned decisions, not implicit prompt drift."))
    parts.append("</svg>\n")
    return "".join(parts)


def render_visual_guide(data: dict[str, Any], output_dir: Path, report_path: Path) -> str:
    image_links = {
        key: _posix_relpath(output_dir / filename, report_path.parent)
        for key, filename in VISUAL_FILENAMES.items()
    }
    image_links.update(
        {
            key: _posix_relpath(output_dir / filename, report_path.parent)
            for key, filename in STATIC_VISUAL_FILENAMES.items()
        }
    )
    counts = data["counts"]
    lines = [
        "# worldquant-harness Visual Guide",
        "",
        "This guide is generated from public harness demo artifacts. It is designed as the fastest path for a new reader to understand worldquant-harness as an agent research harness with memory feedback.",
        "",
        "## Start Here",
        "",
        f"![worldquant-harness overview]({image_links['overview']})",
        "",
        "What this proves: worldquant-harness is a reproducible loop around agent research, presubmit gates, memory, quality review, and profile evolution.",
        "",
        "## Artifact Lifecycle",
        "",
        f"![Artifact lifecycle]({image_links['lifecycle']})",
        "",
        "What this proves: every agent decision is persisted as an auditable artifact before any submit-capable command can be used.",
        "",
        "## Public Demo Trace",
        "",
        f"![Public demo trace]({image_links['trace']})",
        "",
        f"The demo funnel is candidates {counts['candidates']} -> simulated {counts['simulated']} -> ready {counts['ready']} -> submitted {counts['submitted']}. The stable `candidate_uid` links lifecycle events across artifacts.",
        "",
        "## Memory Feedback",
        "",
        f"![Memory feedback graph]({image_links['memory']})",
        "",
        "What this proves: failures and blockers are converted into structured memory instead of being lost in logs.",
        "",
        "## Factor Map",
        "",
        f"![Factor map snapshot]({image_links['factor_map']})",
        "",
        "What this proves: field signatures, source families, and self-correlation pressure make the next synthesis direction explainable.",
        "",
        "## Quality Review",
        "",
        f"![Quality review dashboard]({image_links['quality']})",
        "",
        "What this proves: a time window of generated and submitted alpha quality can be reviewed before changing the research profile.",
        "",
        "## Profile Evolution",
        "",
        f"![Profile evolution timeline]({image_links['profile']})",
        "",
        "What this proves: the next agent profile is a tracked artifact derived from harness metrics.",
        "",
        "## Submit Boundary",
        "",
        f"![Submit boundary]({image_links['submit_boundary']})",
        "",
        "What this proves: public demo, sandbox, and presubmit paths are no-submit by default; real submission requires explicit commands and user credentials.",
        "",
        "## Release Boundary",
        "",
        f"![Release safety boundary]({image_links['release_boundary']})",
        "",
        "What this proves: the public repository should publish the harness and synthetic demo while keeping credentials, raw platform exports, and private research ledgers out of Git.",
        "",
        "## Reproduce",
        "",
        "```powershell",
        "python scripts/run_public_harness_demo.py --output-root reports/public_harness_demo",
        "python scripts/validate_public_harness_artifacts.py reports/public_harness_demo",
        "python scripts/wq_submit_efficiency_report.py `",
        "  --run-roots reports/public_harness_demo `",
        "  --current-name public-demo `",
        "  --output reports/public_harness_demo/efficiency_summary.json `",
        "  --markdown-output reports/public_harness_demo/efficiency_summary.md `",
        "  --events-output reports/public_harness_demo/efficiency_events.jsonl",
        "python scripts/wq_alpha_quality_review.py `",
        "  --reports reports/public_harness_demo `",
        "  --no-platform `",
        "  --no-profile-candidate `",
        "  --output-dir reports/public_harness_demo/quality_review",
        "python scripts/build_public_visual_pack.py --source reports/public_harness_demo --output-dir docs/images --report docs/VISUAL_GUIDE.md",
        "```",
        "",
        "## Artifact To Visual Map",
        "",
        "| Artifact | Visual use |",
        "| --- | --- |",
        "| `demo_summary.json` | overview, submit guard, experiment identity |",
        "| `candidate_specs.jsonl` | candidate count, field map, lifecycle start |",
        "| `presubmit_run/presubmit_ready_sequential.jsonl` | ready lane and accepted candidates |",
        "| `presubmit_run/presubmit_rejected.jsonl` | rejection reasons and blocker memory |",
        "| `evaluations/<eval-id>/eval_summary.json` | harness score, reject counts, field signatures |",
        "| `evaluations/<eval-id>/evolution_result.json` | profile candidate and next experiment |",
        "| `efficiency_summary.json` | candidate_uid funnel and source-family leaderboards |",
        "| `quality_review/summary.json` | period quality dashboard and self-correlation pressure |",
        "| `quality_review/recommended_directions.json` | next synthesis direction callouts |",
        "| `SECURITY.md`, `.gitignore`, release checklist | submit boundary and release boundary |",
        "",
        "## Current Artifact Availability",
        "",
        "| Artifact | Status |",
        "| --- | --- |",
    ]
    for key, status in sorted((data.get("artifacts") or {}).items()):
        lines.append(f"| `{key}` | {status} |")
    lines.append("")
    lines.append("The generated visuals intentionally avoid absolute local paths and private credential material.")
    return "\n".join(lines) + "\n"


def _known_files(source: Path, demo_summary: dict[str, Any], experiment_dir: Path | None) -> dict[str, Path | None]:
    files = _as_dict(demo_summary.get("files"))

    def from_summary(key: str) -> Path | None:
        value = files.get(key)
        if not value:
            return None
        candidate = Path(str(value))
        if candidate.is_file():
            return candidate
        if not candidate.is_absolute() and (source / candidate).is_file():
            return source / candidate
        return None

    eval_dir = _find_eval_dir(experiment_dir)
    return {
        "demo_summary": source / "demo_summary.json",
        "candidate_specs": from_summary("candidate_specs")
        or (experiment_dir / "candidate_specs.jsonl" if experiment_dir else None),
        "ready": from_summary("ready")
        or (experiment_dir / "presubmit_run" / "presubmit_ready_sequential.jsonl" if experiment_dir else None),
        "rejected": from_summary("rejected")
        or (experiment_dir / "presubmit_run" / "presubmit_rejected.jsonl" if experiment_dir else None),
        "eval_summary": from_summary("eval_summary")
        or (eval_dir / "eval_summary.json" if eval_dir else None),
        "evolution_result": from_summary("evolution_result")
        or (eval_dir / "evolution_result.json" if eval_dir else None),
        "efficiency_summary": _first_existing(source / "efficiency_summary.json", source / "root_efficiency_summary.json"),
        "quality_summary": _first_existing(source / "quality_review" / "summary.json", *_quality_summary_candidates(source)),
        "recommended_directions": _first_existing(
            source / "quality_review" / "recommended_directions.json", *_quality_direction_candidates(source)
        ),
    }


def _discover_experiment_dir(source: Path, demo_summary: dict[str, Any]) -> Path | None:
    exp_id = str(demo_summary.get("experiment_id") or "")
    if exp_id and (source / "experiments" / exp_id).is_dir():
        return source / "experiments" / exp_id
    summary_path = str(demo_summary.get("experiment_dir") or "")
    if summary_path and Path(summary_path).is_dir():
        return Path(summary_path)
    experiments_root = source / "experiments"
    if experiments_root.is_dir():
        experiments = sorted(path for path in experiments_root.iterdir() if path.is_dir() and path.name.startswith("exp-"))
        if experiments:
            return experiments[-1]
    return None


def _find_eval_dir(experiment_dir: Path | None) -> Path | None:
    if not experiment_dir:
        return None
    preferred = experiment_dir / "evaluations" / "public-harness-demo"
    if preferred.is_dir():
        return preferred
    evaluations = experiment_dir / "evaluations"
    if evaluations.is_dir():
        dirs = sorted(path for path in evaluations.iterdir() if path.is_dir())
        if dirs:
            return dirs[-1]
    return None


def _quality_summary_candidates(source: Path) -> list[Path]:
    return sorted(source.glob("wq_alpha_quality_review*/summary.json"))


def _quality_direction_candidates(source: Path) -> list[Path]:
    return sorted(source.glob("wq_alpha_quality_review*/recommended_directions.json"))


def _field_rows(
    eval_summary: dict[str, Any],
    efficiency_current: dict[str, Any],
    candidates: list[dict[str, Any]],
    ready: list[dict[str, Any]],
    rejected: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    eval_rows = _as_list((_as_dict(eval_summary.get("field_signature"))).get("rows"))
    if eval_rows:
        return eval_rows
    leaderboard_rows = _leaderboard_rows(efficiency_current, "field_signature")
    if leaderboard_rows:
        return leaderboard_rows
    counts: Counter[str] = Counter()
    ready_counts: Counter[str] = Counter()
    rejected_counts: Counter[str] = Counter()
    for row in candidates:
        counts[str(row.get("field_signature") or _fields_from_expression(str(row.get("expression") or "")) or "unknown")] += 1
    for row in ready:
        ready_counts[str(row.get("field_signature") or _fields_from_expression(str(row.get("expression") or "")) or "unknown")] += 1
    for row in rejected:
        rejected_counts[str(row.get("field_signature") or _fields_from_expression(str(row.get("expression") or "")) or "unknown")] += 1
    keys = sorted(counts or ready_counts or rejected_counts)
    return [
        {
            "field_signature": key,
            "count": counts.get(key, 0),
            "ready_count": ready_counts.get(key, 0),
            "rejected_count": rejected_counts.get(key, 0),
        }
        for key in keys
    ]


def _leaderboard_rows(efficiency_current: dict[str, Any], key: str) -> list[dict[str, Any]]:
    leaderboards = _as_dict(efficiency_current.get("leaderboards"))
    rows = _as_list(leaderboards.get(key))
    return [row for row in rows if isinstance(row, dict)]


def _quality_summary(summary: dict[str, Any], directions: list[Any]) -> dict[str, Any]:
    metrics = _as_dict(summary.get("metrics"))
    pressure = _as_list(summary.get("self_correlation_pressure"))
    return {
        "available": bool(summary),
        "metrics": metrics,
        "pressure": [row for row in pressure if isinstance(row, dict)],
        "directions": [row for row in directions if isinstance(row, dict)],
    }


def _profile_summary(evolution: dict[str, Any]) -> dict[str, Any]:
    next_generation = _as_dict(evolution.get("next_generation"))
    profile_evolution = _as_dict(next_generation.get("profile_evolution"))
    recommended_key = str(profile_evolution.get("recommended_candidate") or "")
    candidates = _as_dict(profile_evolution.get("candidates"))
    recommended = _as_dict(candidates.get(recommended_key)) if recommended_key else {}
    if not recommended and candidates:
        first_key = sorted(candidates)[0]
        recommended_key = first_key
        recommended = _as_dict(candidates.get(first_key))
    actions = [
        f"{row.get('trigger')}: {row.get('change')}"
        for row in _as_list(recommended.get("actions"))
        if isinstance(row, dict) and row.get("trigger") and row.get("change")
    ]
    profile = _as_dict(recommended.get("profile"))
    child = _as_dict(next_generation.get("child_experiment"))
    return {
        "available": bool(evolution and next_generation),
        "baseline_score": _float(profile_evolution.get("baseline_score"), next_generation.get("harness_score")),
        "recommended_candidate": recommended_key,
        "actions": actions,
        "priority_biases": _as_list(profile.get("priority_biases")),
        "child_experiment": child.get("experiment_id") or Path(str(child.get("experiment") or "")).parent.name or "",
    }


def _svg_header(width: int, height: int, title: str) -> str:
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">
<title id="title">{escape(title)}</title>
<desc id="desc">Static worldquant-harness public harness visual.</desc>
<defs>
  <marker id="arrow" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto" markerUnits="strokeWidth">
    <path d="M0,0 L0,6 L9,3 z" fill="{COLOR["muted"]}"/>
  </marker>
  <style>
    text {{ font-family: Inter, Segoe UI, Arial, sans-serif; fill: {COLOR["ink"]}; letter-spacing: 0; }}
    .title {{ font-size: 28px; font-weight: 700; }}
    .subtitle {{ font-size: 15px; fill: {COLOR["muted"]}; }}
    .label {{ font-size: 15px; font-weight: 700; }}
    .body {{ font-size: 13px; fill: {COLOR["muted"]}; }}
    .tiny {{ font-size: 12px; fill: {COLOR["muted"]}; }}
    .metric {{ font-size: 20px; font-weight: 700; }}
  </style>
</defs>
<rect width="100%" height="100%" fill="{COLOR["white"]}"/>
'''


def _title(x: int, y: int, text: str) -> str:
    return f'<text x="{x}" y="{y}" class="title">{escape(text)}</text>'


def _subtitle(x: int, y: int, text: str) -> str:
    return f'<text x="{x}" y="{y}" class="subtitle">{escape(text)}</text>'


def _node(x: float, y: float, width: float, height: float, title: str, body: str, fill: str, stroke: str) -> str:
    lines = _wrap(body, 30)
    out = [f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="8" fill="{fill}" stroke="{stroke}" stroke-width="2"/>']
    out.append(f'<text x="{x + 16}" y="{y + 30}" class="label">{escape(_short(title, 28))}</text>')
    for i, line in enumerate(lines[:3]):
        out.append(f'<text x="{x + 16}" y="{y + 56 + i * 18}" class="body">{escape(line)}</text>')
    return "".join(out)


def _arrow(x1: float, y1: float, x2: float, y2: float, stroke: str | None = None) -> str:
    stroke = stroke or COLOR["muted"]
    return f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{stroke}" stroke-width="2.2" marker-end="url(#arrow)"/>'


def _metric_card(x: float, y: float, title: str, value: str, desc: str, color: str) -> str:
    return (
        f'<rect x="{x}" y="{y}" width="206" height="104" rx="8" fill="{COLOR["panel"]}" stroke="{COLOR["line"]}"/>'
        f'<text x="{x + 16}" y="{y + 28}" class="body">{escape(title)}</text>'
        f'<text x="{x + 16}" y="{y + 61}" class="metric" fill="{color}">{escape(value)}</text>'
        f'<text x="{x + 16}" y="{y + 84}" class="tiny">{escape(_short(desc, 30))}</text>'
    )


def _callout(x: float, y: float, width: float, height: float, title: str, body: str) -> str:
    return (
        f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="8" fill="{COLOR["panel"]}" stroke="{COLOR["line"]}"/>'
        f'<text x="{x + 16}" y="{y + 22}" class="label">{escape(title)}</text>'
        f'<text x="{x + 16}" y="{y + 42}" class="tiny">{escape(_short(body, 150))}</text>'
    )


def _empty_panel(x: float, y: float, width: float, height: float, message: str) -> str:
    return (
        f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="8" fill="{COLOR["panel"]}" stroke="{COLOR["line"]}"/>'
        f'<text x="{x + width / 2}" y="{y + height / 2}" text-anchor="middle" class="label">{escape(message)}</text>'
        f'<text x="{x + width / 2}" y="{y + height / 2 + 28}" text-anchor="middle" class="tiny">not available in this demo</text>'
    )


def _mini_bar_chart(x: float, y: float, width: float, height: float, values: dict[str, Any], title: str) -> str:
    clean = [(str(k), _float(v, 0.0) or 0.0) for k, v in values.items()]
    clean = clean[:5]
    max_value = max([v for _, v in clean] + [1.0])
    out = [f'<text x="{x}" y="{y}" class="label">{escape(title)}</text>']
    bar_y = y + 24
    for label, value in clean:
        out.append(f'<text x="{x}" y="{bar_y + 15}" class="tiny">{escape(_short(label, 24))}</text>')
        out.append(f'<rect x="{x + 150}" y="{bar_y}" width="{width - 180}" height="18" rx="5" fill="{COLOR["gray"]}"/>')
        out.append(
            f'<rect x="{x + 150}" y="{bar_y}" width="{max((width - 180) * value / max_value, 3):.1f}" height="18" rx="5" fill="{COLOR["blue"]}"/>'
        )
        out.append(f'<text x="{x + width - 18}" y="{bar_y + 15}" text-anchor="end" class="tiny">{escape(_fmt_metric(value))}</text>')
        bar_y += 28
    return "".join(out)


def _legend(x: float, y: float, items: list[tuple[str, str]]) -> str:
    out = []
    for i, (label, color) in enumerate(items):
        yy = y + i * 24
        out.append(f'<circle cx="{x}" cy="{yy}" r="6" fill="{color}"/>')
        out.append(f'<text x="{x + 16}" y="{yy + 5}" class="tiny">{escape(label)}</text>')
    return "".join(out)


def _footer(x: float, y: float, text: str) -> str:
    return f'<text x="{x}" y="{y}" class="tiny">{escape(text)}</text>'


def _read_json(path: Path | None) -> dict[str, Any]:
    if not path or not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _read_jsonl(path: Path | None) -> list[dict[str, Any]]:
    if not path or not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _read_csv(path: Path | None) -> list[dict[str, Any]]:
    if not path or not path.is_file():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _first_existing(*paths: Path) -> Path | None:
    for path in paths:
        if path and path.is_file():
            return path
    return None


def _int(*values: Any) -> int:
    for value in values:
        if value is None:
            continue
        try:
            return int(float(value))
        except (TypeError, ValueError):
            continue
    return 0


def _float(*values: Any) -> float | None:
    for value in values:
        if value is None or value == "":
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _fmt_score(value: Any) -> str:
    number = _float(value)
    return "n/a" if number is None else f"{number:.3f}"


def _fmt_pct(value: Any) -> str:
    number = _float(value)
    return "n/a" if number is None else f"{number:.0%}"


def _fmt_metric(value: float) -> str:
    if abs(value) <= 1:
        return f"{value:.0%}"
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.2f}"


def _fields_from_expression(expression: str) -> str:
    fields = sorted(set(re.findall(r"\b(close|open|high|low|returns|volume|vwap|adv20|not_a_real_field)\b", expression)))
    return "|".join(fields)


def _first_direction(data: dict[str, Any]) -> str:
    directions = data.get("quality", {}).get("directions") or []
    if directions:
        title = str(directions[0].get("title") or "")
        if title and title.isascii():
            return title
        blocker = str(directions[0].get("expected_blocker") or "")
        if blocker:
            return f"{blocker.replace('_', '-')} repair direction"
    return "recommended synthesis direction"


def _artifact_status(path: Path | None) -> str:
    return "available" if path and path.is_file() else "not available in this demo"


def _display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT.resolve())).replace("\\", "/")
    except ValueError:
        return path.name


def _posix_relpath(path: Path, start: Path) -> str:
    return os.path.relpath(path, start).replace("\\", "/")


def _short(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: max(0, limit - 3)] + "..."


def _wrap(text: str, width: int) -> list[str]:
    words = str(text).split()
    if not words:
        return [""]
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        if len(current) + len(word) + 1 <= width:
            current += " " + word
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _assert_public_content(content: str, path: Path) -> None:
    for pattern in PRIVATE_PATH_PATTERNS:
        match = pattern.search(content)
        if match:
            raise ValueError(f"refusing to write private path pattern to {path}: {match.group(0)!r}")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build worldquant-harness public harness visual pack")
    parser.add_argument("--source", default="reports/public_harness_demo", help="Public demo artifact root")
    parser.add_argument("--output-dir", default="docs/images", help="Directory for generated SVG files")
    parser.add_argument("--report", default="docs/VISUAL_GUIDE.md", help="Generated Markdown visual guide")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    result = build_public_visual_pack(args.source, args.output_dir, args.report)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
