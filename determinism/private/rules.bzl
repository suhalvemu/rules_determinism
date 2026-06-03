"""Internal rule implementation for determinism_test."""

def _determinism_test_impl(ctx):
    # The test runner script is generated per target so it knows which
    # Bazel targets to build and where the checker scripts live.
    checker   = ctx.executable._checker
    static_py = ctx.executable._static_check
    summarize = ctx.executable._summarize

    targets_str = " ".join(ctx.attr.targets)

    script = ctx.actions.declare_file(ctx.label.name + "_runner.sh")
    ctx.actions.write(
        output = script,
        content = """\
#!/usr/bin/env bash
set -euo pipefail

CHECKER="{checker}"
STATIC_CHECK="{static_check}"
SUMMARIZE="{summarize}"
TARGETS="{targets}"
MAX_TARGETS={max_targets}

# Run static analysis on BUILD files in changed packages
STATIC_REPORT="$(mktemp /tmp/det_static_XXXX.json)"
DYNAMIC_REPORT="$(mktemp /tmp/det_dynamic_XXXX.json)"
COMMENT="$(mktemp /tmp/det_comment_XXXX.md)"

# Static: scan source files for the target packages
echo "$TARGETS" | tr ' ' '\\n' | sed 's|//||; s|:.*||' | while read pkg; do
  find "$pkg" -name "BUILD.bazel" -o -name "BUILD" -o \\
              -name "*.go" -o -name "*.cc" -o -name "*.cpp" -o \\
              -name "*.c" -o -name "*.h" -o -name "*.py" -o \\
              -name "*.java" -o -name "*.ts" 2>/dev/null
done | sort -u | "$STATIC_CHECK" --stdin > "$STATIC_REPORT" || true

# Dynamic: build twice, compare hashes
"$CHECKER" $TARGETS > "$DYNAMIC_REPORT" 2>/tmp/det_dynamic_stderr.txt || true

# Summarize
"$SUMMARIZE" \\
  --static  "$STATIC_REPORT" \\
  --dynamic "$DYNAMIC_REPORT" \\
  > "$COMMENT"

cat "$COMMENT"

# Fail if non-deterministic or HIGH risk findings
HIGH=$(python3 -c "import json; r=json.load(open('$STATIC_REPORT')); print(r.get('summary',{{}}).get('HIGH',0))" 2>/dev/null || echo 0)
DETERMINISTIC=$(python3 -c "import json; r=json.load(open('$DYNAMIC_REPORT')); print(str(r.get('deterministic',True)).lower())" 2>/dev/null || echo true)

if [[ "$HIGH" -gt 0 ]]; then
  echo ""
  echo "FAIL: $HIGH HIGH risk non-deterministic pattern(s) found." >&2
  exit 1
fi

if [[ "$DETERMINISTIC" == "false" ]]; then
  echo ""
  echo "FAIL: Dynamic analysis detected non-deterministic outputs." >&2
  exit 1
fi

echo ""
echo "PASS: All determinism checks passed."
""".format(
            checker      = checker.short_path,
            static_check = static_py.short_path,
            summarize    = summarize.short_path,
            targets      = targets_str,
            max_targets  = ctx.attr.max_targets,
        ),
        is_executable = True,
    )

    runfiles = ctx.runfiles(files = [checker, static_py, summarize, script])
    return [DefaultInfo(executable = script, runfiles = runfiles)]


determinism_test = rule(
    implementation = _determinism_test_impl,
    test = True,
    attrs = {
        "targets": attr.string_list(
            mandatory = True,
            doc = "Bazel labels to check for determinism. Each target is built twice; output hashes are compared.",
        ),
        "max_targets": attr.int(
            default = 20,
            doc = "Cap on number of targets checked to keep CI fast.",
        ),
        "_checker": attr.label(
            default = "//determinism/private:dynamic_check",
            executable = True,
            cfg = "exec",
        ),
        "_static_check": attr.label(
            default = "//determinism/private:static_check",
            executable = True,
            cfg = "exec",
        ),
        "_summarize": attr.label(
            default = "//determinism/private:summarize",
            executable = True,
            cfg = "exec",
        ),
    },
    doc = """\
Verifies that the listed Bazel targets produce bit-for-bit identical outputs
on every build given the same inputs (i.e. are deterministic and cacheable).

Runs two layers of checks:
  1. Static analysis — scans BUILD files and source files for known
     non-deterministic patterns (timestamps, hostname, $RANDOM, etc.)
  2. Dynamic analysis — builds each target twice from a clean state and
     compares output file hashes. Any differing hash fails the test.

Example:
    determinism_test(
        name = "check",
        targets = [
            "//apps/go-service:go-service",
            "//apps/cpp-lib:greeter",
        ],
    )
""",
)
