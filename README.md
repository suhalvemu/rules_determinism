# rules_determinism

[![CI](https://github.com/suhalvemu/rules_determinism/actions/workflows/ci.yml/badge.svg)](https://github.com/suhalvemu/rules_determinism/actions/workflows/ci.yml)

Bazel rules that catch non-deterministic build actions before they merge — protecting your remote cache hit rate.

## The Problem

A single non-deterministic genrule can collapse your Bazel cache hit rate from 90% to 20% overnight. It produces different output bytes on every run, so every build is a cache miss — for every engineer, on every CI run.

The tricky part: it doesn't fail your tests. Builds complete. Everything looks green. You only notice days later when CI is mysteriously slow.

`rules_determinism` catches it at PR time, before it merges.

## How It Works

Two layers run on every PR:

**Layer 1 — Static analysis (fast, ~2s)**
Scans changed BUILD files and source files for 16 known non-deterministic patterns:
- `$(date)`, `$(hostname)`, `$RANDOM` in genrule commands
- `__DATE__` / `__TIME__` macros in C/C++
- `time.Now()` at package init level in Go
- `datetime.now()` at module level in Python
- `System.currentTimeMillis()` in Java static initialisers
- Over-broad `glob(["**/*"])` patterns
- Network calls (`curl`, `wget`) in genrules

**Layer 2 — Dynamic analysis (definitive proof)**
Builds the affected targets twice from a clean state using isolated disk caches. Compares output file hashes between run 1 and run 2. Any differing hash = non-deterministic action found.

```
Build run 1 → hash outputs → store in cache A
bazel clean
Build run 2 → hash outputs → store in cache B
diff cache A vs cache B
→ identical: ✅ deterministic
→ differs:   ❌ non-deterministic — PR blocked
```

## Installation

Add to your `MODULE.bazel`:

```python
bazel_dep(name = "rules_determinism", version = "0.1.0")
```

## Usage

### As a Bazel test target

```python
# BUILD.bazel
load("@rules_determinism//determinism:defs.bzl", "determinism_test")

determinism_test(
    name = "check_determinism",
    targets = [
        "//apps/go-service:go-service",
        "//apps/cpp-lib:greeter",
    ],
)
```

```bash
bazel test //:check_determinism
```

### As a GitHub Actions reusable workflow

In your repo's PR workflow:

```yaml
# .github/workflows/pr.yml
jobs:
  determinism:
    uses: suhalvemu/rules_determinism/.github/workflows/pr-determinism-check.yml@main
    with:
      bazel_targets: "//apps/go-service //apps/cpp-lib"
    secrets:
      buildbuddy_api_key: ${{ secrets.BUILDBUDDY_API_KEY }}
```

Or let it auto-detect changed packages (no `bazel_targets` needed):

```yaml
jobs:
  determinism:
    uses: suhalvemu/rules_determinism/.github/workflows/pr-determinism-check.yml@main
    secrets:
      buildbuddy_api_key: ${{ secrets.BUILDBUDDY_API_KEY }}
```

## PR Comment Output

On a PR with a non-deterministic pattern, you'll see:

```
## 🔴 Determinism Check FAILED

### Static Analysis — 🔴 Issues found
| File              | Line | Risk    | Issue                                   | Suggestion                        |
|-------------------|------|---------|------------------------------------------|-----------------------------------|
| tools/BUILD.bazel | 15   | 🔴 HIGH | Embeds timestamp via $(date)             | Use --stamp with STABLE_GIT_COMMIT|

### Dynamic Analysis — ✅ Deterministic
All 5 output hashes matched between run 1 and run 2.
```

## Gate Behaviour

| Finding | Default behaviour |
|---------|-----------------|
| HIGH static finding | ❌ PR blocked |
| Dynamic outputs differ | ❌ PR blocked |
| MEDIUM static finding | ⚠️ Warning only |
| LOW static finding | ⚠️ Warning only |

Configure via workflow inputs (`fail_on_static_high`, `fail_on_dynamic`).

## Opt Out

To suppress a false positive on a specific line, add a comment:

```python
genrule(
    name = "intentional",
    cmd = "date > $@",  # determinism-check: ignore
)
```

## Why Cache Hit Rate Matters

| Cache hit rate | Build time (50-engineer team) | CI compute cost |
|---------------|------------------------------|-----------------|
| 90%+ | Fast | Baseline |
| 50% | 5x slower | 5x more expensive |
| 20% | 12x slower | 12x more expensive |

One non-deterministic action in a shared library can cascade to hundreds of dependent targets, all missing on every run.

## Background

This ruleset emerged from real Engineering Productivity work building Bazel infrastructure for polyglot monorepos. The problem is well-understood but no automated PR-level tool existed — Bazel's own test suite uses the "build twice" pattern internally, but it was never packaged for external use.

## Author

**Suhal Vemu** — Senior Software Engineer, Engineering Productivity
- GitHub: [@suhalvemu](https://github.com/suhalvemu)
- LinkedIn: [linkedin.com/in/suhalvemu](https://linkedin.com/in/suhalvemu)

Questions, issues, contributions welcome.

## License

Apache 2.0
