#!/usr/bin/env python3
"""
Static analysis scanner for non-deterministic Bazel build patterns.

Scans changed BUILD files and source files for patterns that commonly
cause cache misses. Outputs a JSON report consumed by summarize.py.

Usage:
    python3 static_check.py --files file1 file2 ...
    git diff --name-only origin/main | python3 static_check.py --stdin
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import NamedTuple

# ---------------------------------------------------------------------------
# Pattern definitions
# ---------------------------------------------------------------------------

class Pattern(NamedTuple):
    regex: str
    risk: str           # HIGH | MEDIUM | LOW
    description: str
    suggestion: str
    file_types: list    # extensions this pattern applies to


PATTERNS = [
    # ── BUILD file patterns ────────────────────────────────────────────────

    Pattern(
        regex=r'\$\(date\b|\$\{date\b|`date\b',
        risk="HIGH",
        description="Embeds current timestamp via shell `date` — output changes every second",
        suggestion="Use --stamp with STABLE_GIT_COMMIT in workspace_status.sh, or remove timestamp entirely",
        file_types=["BUILD", "BUILD.bazel", ".bzl"],
    ),
    Pattern(
        regex=r'\$\(hostname\)|\$\{hostname\}|`hostname`',
        risk="HIGH",
        description="Embeds machine hostname — output differs on every machine",
        suggestion="Remove hostname from output or replace with a fixed string",
        file_types=["BUILD", "BUILD.bazel", ".bzl"],
    ),
    Pattern(
        regex=r'\$\(whoami\)|\$\{whoami\}|`whoami`',
        risk="HIGH",
        description="Embeds current username — output differs per developer",
        suggestion="Remove username from output",
        file_types=["BUILD", "BUILD.bazel", ".bzl"],
    ),
    Pattern(
        regex=r'\$RANDOM|\$\{RANDOM\}',
        risk="HIGH",
        description="Uses shell $RANDOM — produces different value every run",
        suggestion="Use a fixed seed or remove randomness from build output",
        file_types=["BUILD", "BUILD.bazel", ".bzl"],
    ),
    Pattern(
        regex=r'\$\$PID|\$PID\b|\$\{PID\}',
        risk="HIGH",
        description="Embeds process ID — different on every run",
        suggestion="Remove PID from build output",
        file_types=["BUILD", "BUILD.bazel", ".bzl"],
    ),
    Pattern(
        regex=r'curl\s+|wget\s+',
        risk="HIGH",
        description="Network call in genrule — output depends on external service, build is not hermetic",
        suggestion="Declare the downloaded file as a repository_rule input or http_file, not a genrule cmd",
        file_types=["BUILD", "BUILD.bazel", ".bzl"],
    ),
    Pattern(
        regex=r'glob\(\s*\[[\s"\']*\*\*[/\*]',
        risk="MEDIUM",
        description="Over-broad glob (\"**/*\") — any file change in the tree invalidates this target",
        suggestion="Scope the glob to specific extensions: glob([\"src/**/*.cc\"]) or list files explicitly",
        file_types=["BUILD", "BUILD.bazel"],
    ),
    Pattern(
        regex=r'cmd\s*=\s*["\'].*\b(python|python3|bash|sh|node)\b(?!\s+\$\(location)',
        risk="MEDIUM",
        description="Tool called from PATH in genrule — version may differ between machines",
        suggestion="Declare the tool as a Bazel target and reference it with $(location //path:tool)",
        file_types=["BUILD", "BUILD.bazel"],
    ),
    Pattern(
        regex=r'cmd\s*=\s*["\'].*\/tmp\/',
        risk="MEDIUM",
        description="genrule reads from /tmp — content outside the Bazel sandbox",
        suggestion="Pass all inputs through declared srcs; never read from /tmp directly",
        file_types=["BUILD", "BUILD.bazel"],
    ),
    Pattern(
        regex=r'action_env\s*=\s*\[',
        risk="LOW",
        description="action_env passes environment variables into actions — verify all vars are pinned to fixed values",
        suggestion="Use name=value pairs (e.g. FOO=bar) not bare names, so the value is fixed not machine-dependent",
        file_types=["BUILD", "BUILD.bazel", ".bazelrc"],
    ),

    # ── C / C++ source patterns ────────────────────────────────────────────

    Pattern(
        regex=r'\b__DATE__\b|\b__TIME__\b|\b__TIMESTAMP__\b',
        risk="HIGH",
        description="C/C++ compiler macros __DATE__, __TIME__, __TIMESTAMP__ embed build time — different output every run",
        suggestion="Remove macro usage, or pass a fixed version string via -D flag from a stamped genrule",
        file_types=[".c", ".cc", ".cpp", ".cxx", ".h", ".hpp"],
    ),
    Pattern(
        regex=r'std::chrono::.*now\(\)|std::time\(|time\(NULL\)|time\(nullptr\)',
        risk="MEDIUM",
        description="Runtime clock call at static initialisation time embeds timestamp in object",
        suggestion="Initialise time values at runtime (main() or lazy init), not at static init time",
        file_types=[".c", ".cc", ".cpp", ".cxx"],
    ),

    # ── Go source patterns ─────────────────────────────────────────────────

    Pattern(
        regex=r'time\.Now\(\)|time\.Since\(',
        risk="MEDIUM",
        description="time.Now() / time.Since() in package-level var or init() embeds timestamp in binary",
        suggestion="Move to runtime initialisation inside main() or a lazy once.Do()",
        file_types=[".go"],
    ),
    Pattern(
        regex=r'^var\s+\w+\s*=\s*rand\.',
        risk="HIGH",
        description="Package-level rand initialisation without fixed seed — non-deterministic",
        suggestion="Use rand.New(rand.NewSource(fixedSeed)) or initialise inside main()",
        file_types=[".go"],
    ),

    # ── Python source patterns ─────────────────────────────────────────────

    Pattern(
        regex=r'^(?!#).*datetime\.now\(\)|^(?!#).*datetime\.utcnow\(\)',
        risk="MEDIUM",
        description="datetime.now() at module level embeds timestamp when module is compiled to .pyc",
        suggestion="Move to a function, not module-level code",
        file_types=[".py"],
    ),
    Pattern(
        regex=r'^(?!#).*os\.getenv\(|^(?!#).*os\.environ\[',
        risk="LOW",
        description="os.getenv / os.environ at module level reads machine environment into compiled output",
        suggestion="Read env vars inside functions, or declare them via --action_env in .bazelrc",
        file_types=[".py"],
    ),

    # ── Java source patterns ───────────────────────────────────────────────

    Pattern(
        regex=r'System\.currentTimeMillis\(\)|System\.nanoTime\(\)|new\s+Date\(\)',
        risk="MEDIUM",
        description="Timestamp call in static initialiser embeds build time into class file",
        suggestion="Move to instance initialisation or a factory method called at runtime",
        file_types=[".java"],
    ),
]

# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

def _matches_file_type(path: Path, file_types: list) -> bool:
    """Check if a file matches the given file type list.

    Handles:
    - Exact name match:   BUILD, BUILD.bazel, .bazelrc
    - Extension match:    .go, .cc, .py
    - Suffix pattern:     any file ending in BUILD.bazel or BUILD
    """
    name   = path.name
    suffix = path.suffix
    if name in file_types or suffix in file_types:
        return True
    # Match files named BUILD or ending in /BUILD, BUILD.bazel etc.
    for ft in file_types:
        if not ft.startswith(".") and (name == ft or name.endswith(ft)):
            return True
    return False


def scan_file(path: Path) -> list[dict]:
    findings = []

    applicable = [p for p in PATTERNS if _matches_file_type(path, p.file_types)]
    if not applicable:
        return findings

    try:
        lines = path.read_text(errors="replace").splitlines()
    except (OSError, PermissionError) as e:
        return [{"file": str(path), "error": str(e)}]

    is_bazelrc = path.name == ".bazelrc"
    for lineno, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped.startswith("#") and not is_bazelrc:
            continue  # skip comment lines in source files
        for pattern in applicable:
            if re.search(pattern.regex, line):
                findings.append({
                    "file": str(path),
                    "line": lineno,
                    "content": line.rstrip(),
                    "risk": pattern.risk,
                    "description": pattern.description,
                    "suggestion": pattern.suggestion,
                })
    return findings


def main():
    parser = argparse.ArgumentParser(description="Static determinism checker for Bazel repos")
    parser.add_argument("--files", nargs="*", default=[], help="Files to scan")
    parser.add_argument("--stdin", action="store_true", help="Read file list from stdin (one per line)")
    parser.add_argument("--repo-root", default=".", help="Repo root (for relative path display)")
    args = parser.parse_args()

    files = list(args.files)
    if args.stdin:
        files += [line.strip() for line in sys.stdin if line.strip()]

    if not files:
        print(json.dumps({"findings": [], "summary": {"HIGH": 0, "MEDIUM": 0, "LOW": 0}}))
        return

    all_findings = []
    for f in files:
        path = Path(f)
        if path.exists() and path.is_file():
            all_findings.extend(scan_file(path))

    summary = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    for finding in all_findings:
        risk = finding.get("risk", "LOW")
        summary[risk] = summary.get(risk, 0) + 1

    report = {
        "findings": all_findings,
        "summary": summary,
        "files_scanned": len(files),
    }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
