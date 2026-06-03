#!/usr/bin/env python3
"""
summarize.py — Combine static + dynamic JSON reports into a GitHub PR comment.

Usage:
    python3 summarize.py \
        --static  /tmp/static_report.json \
        --dynamic /tmp/dynamic_report.json \
        --pr-number 42 \
        --commit abc123

Output: markdown written to stdout (pipe to gh pr comment or a file).
"""

import argparse
import json
import sys
from pathlib import Path

RISK_EMOJI = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🔵"}
RISK_ORDER  = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}


def load_json(path: str) -> dict:
    try:
        return json.loads(Path(path).read_text())
    except Exception as e:
        return {"error": str(e)}


def static_section(report: dict) -> str:
    findings = report.get("findings", [])
    summary  = report.get("summary", {})
    scanned  = report.get("files_scanned", 0)

    if report.get("error"):
        return f"### Static Analysis\n\n⚠️ Error running static check: `{report['error']}`\n"

    high   = summary.get("HIGH", 0)
    medium = summary.get("MEDIUM", 0)
    low    = summary.get("LOW", 0)
    total  = high + medium + low

    if total == 0:
        return (
            f"### Static Analysis — ✅ Clean\n\n"
            f"Scanned {scanned} file(s). No known non-deterministic patterns found.\n"
        )

    verdict = "🔴 Issues found" if high > 0 else ("🟡 Warnings found" if medium > 0 else "🔵 Notes")
    lines = [
        f"### Static Analysis — {verdict}\n",
        f"Scanned {scanned} file(s). Found {high} HIGH, {medium} MEDIUM, {low} LOW risk pattern(s).\n",
        "| File | Line | Risk | Issue | Suggestion |",
        "|------|------|------|-------|------------|",
    ]

    sorted_findings = sorted(findings, key=lambda f: RISK_ORDER.get(f.get("risk","LOW"), 2))
    for f in sorted_findings:
        emoji   = RISK_EMOJI.get(f.get("risk", "LOW"), "🔵")
        risk    = f.get("risk", "LOW")
        file_   = f.get("file", "?")
        line    = f.get("line", "?")
        desc    = f.get("description", "")
        suggest = f.get("suggestion", "")
        content = f.get("content", "").strip()[:60]
        lines.append(f"| `{file_}:{line}` | `{content}` | {emoji} {risk} | {desc} | {suggest} |")

    return "\n".join(lines) + "\n"


def dynamic_section(report: dict) -> str:
    if report.get("error") and report["error"] != "null":
        return f"### Dynamic Analysis\n\n⚠️ Error running dynamic check: `{report['error']}`\n"

    deterministic  = report.get("deterministic", True)
    targets        = report.get("targets_checked", [])
    differing      = report.get("differing_output_files", [])
    run1_count     = report.get("run1_output_files_hashed", 0)
    cache_hits     = report.get("run2_cache_hits_against_run1", 0)

    if not targets:
        return (
            "### Dynamic Analysis — ⏭️ Skipped\n\n"
            "No buildable targets found in changed packages, or target cap exceeded.\n"
        )

    if deterministic:
        lines = [
            "### Dynamic Analysis — ✅ Deterministic\n",
            f"Built {len(targets)} target(s) twice from a clean state.\n",
            f"All {run1_count} output file hashes matched between run 1 and run 2.",
            f"Run 2 disk cache hits against run 1: **{cache_hits}** (proves outputs are identical).\n",
        ]
        return "\n".join(lines) + "\n"
    else:
        lines = [
            "### Dynamic Analysis — 🔴 Non-Deterministic Outputs Detected\n",
            f"Built {len(targets)} target(s) twice. The following output files had **different hashes** between run 1 and run 2:\n",
        ]
        for f in differing:
            lines.append(f"- `{f}`")
        lines += [
            "",
            "This means at least one build action produces different bytes given identical inputs.",
            "Check the offending targets with:",
            "```bash",
            "bazel build <target> --execution_log_compact_file=/tmp/run1.log",
            "bazel clean",
            "bazel build <target> --execution_log_compact_file=/tmp/run2.log",
            "bazel-execlog diff /tmp/run1.log /tmp/run2.log",
            "```",
        ]
        return "\n".join(lines) + "\n"


def overall_verdict(static_report: dict, dynamic_report: dict) -> str:
    static_high    = static_report.get("summary", {}).get("HIGH", 0)
    static_any     = sum(static_report.get("summary", {}).values())
    dynamic_clean  = dynamic_report.get("deterministic", True)
    dynamic_ran    = bool(dynamic_report.get("targets_checked"))

    if static_high > 0 or (dynamic_ran and not dynamic_clean):
        return "## 🔴 Determinism Check FAILED"
    elif static_any > 0:
        return "## 🟡 Determinism Check — Warnings"
    else:
        return "## ✅ Determinism Check Passed"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--static",    required=True)
    parser.add_argument("--dynamic",   required=True)
    parser.add_argument("--pr-number", default="")
    parser.add_argument("--commit",    default="")
    args = parser.parse_args()

    static_report  = load_json(args.static)
    dynamic_report = load_json(args.dynamic)

    parts = [
        overall_verdict(static_report, dynamic_report),
        "",
        f"Commit: `{args.commit}`" if args.commit else "",
        "",
        static_section(static_report),
        dynamic_section(dynamic_report),
        "---",
        "_This check is automated. "
        "See [determinism checker design](../docs/determinism-checker-design.md) for how it works. "
        "False positives? Add `# determinism-check: ignore` on the offending line._",
    ]

    print("\n".join(p for p in parts))


if __name__ == "__main__":
    main()
