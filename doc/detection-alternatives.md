# Non-Determinism Detection: Alternatives to Build-Twice

The current `rules_determinism` dynamic checker uses a "build twice, compare hashes" approach. This is definitive proof of non-determinism but forces two full cold builds per PR — too expensive for any significant repo.

This document surveys alternatives. Each approach is evaluated on three axes:

- **Cost** — how many extra builds / how much CI overhead
- **Signal quality** — how often does it catch real non-determinism vs miss it or false-positive
- **Complexity** — how hard to implement and maintain

---

## Current approach: Build Twice (baseline)

Build the affected targets twice with isolated disk caches. Compare output file hashes.

```
Run 1: bazel build //... --disk_cache=/tmp/cache-a  → hash outputs → store
Run 2: bazel build //... --disk_cache=/tmp/cache-b  → hash outputs → store
diff cache-a-hashes vs cache-b-hashes
→ any difference = non-deterministic
```

| Attribute | Value |
|-----------|-------|
| Cost | 2× build time per PR |
| Signal quality | Definitive (no false positives, no false negatives for action-level issues) |
| Complexity | Low |
| Toolchain non-determinism? | Catches it |
| Misses | Flaky non-determinism (only manifests sometimes) |

**Problem**: On a large monorepo with 50+ engineers, doubling every PR build time is not acceptable (Brian Chesko, Bazel #rules, June 2026).

**Best trigger for this approach**: Nightly on `main`, not per-PR.

---

## Alternative 1: Golden Hash Baseline

**Idea**: Store output hashes from the last known-good build of `main`. On each PR, compare output hashes of *unchanged* targets against that golden baseline. One build per PR.

### How it works

```
Nightly (main):
  bazel build //...
  bazel cquery //... --output=files | xargs sha256sum > golden-hashes.txt
  upload golden-hashes.txt to GCS / GitHub artifact

On each PR:
  bazel build //changed-targets... + their dependents
  For targets NOT touched by this PR:
    compare their output hashes against golden-hashes.txt
    any mismatch = non-determinism introduced in a previous commit
```

### Strengths
- One build per PR (normal CI cost)
- Catches non-determinism in targets unrelated to the PR change
- Hash baseline is a permanent audit record

### Weaknesses
- Only catches non-determinism in *unchanged* targets — if the PR itself introduces non-determinism, you won't see it until the *next* PR (which doesn't touch those targets)
- Baseline can become stale if a toolchain version bumps legitimately change all hashes
- Requires external storage (GCS bucket, or GitHub Actions artifact with long retention)

### Implementation sketch
```python
# Compare current build hashes against stored golden
def compare_against_golden(current_hashes: dict, golden_hashes: dict, changed_targets: set):
    for target, hash_val in current_hashes.items():
        if target in changed_targets:
            continue  # skip — PR touched this target
        if target in golden_hashes and golden_hashes[target] != hash_val:
            yield NonDeterministicTarget(target, golden_hashes[target], hash_val)
```

### Cost / signal

| Attribute | Value |
|-----------|-------|
| Cost | 1× build per PR + nightly golden build |
| Signal quality | Good — catches persistent non-determinism. Misses PR-introduced issues until next PR. |
| Complexity | Medium — requires storage and baseline management |

---

## Alternative 2: Cache Miss Rate Monitoring (passive)

**Idea**: Parse build events from BES (Build Event Service) after each run. Track cache hit rate per target over time. A sudden drop in hit rate for a target = non-determinism likely introduced.

No extra builds. Pure observation.

### How it works

BuildBuddy, EngFlow, and Bazel Remote Cache all emit build events with per-action cache hit/miss data. You can:

1. Consume the Build Event Protocol stream (`--bes_backend`) after each CI build
2. Record per-target cache hit rates in a time-series store (Prometheus, BigQuery, or even a plain JSON log)
3. Alert when a target's rolling hit rate drops below a threshold (e.g. 80% → 30% in 24h)

```
Build N:   //apps/cpp-lib:greeter  → cache hit  ✅  (100% last 30 builds)
Build N+1: //apps/cpp-lib:greeter  → cache miss ❌  (rate drops to 50%)
Build N+2: //apps/cpp-lib:greeter  → cache miss ❌  (rate drops to 0%)
→ Alert: "//apps/cpp-lib:greeter hit rate degraded — possible non-determinism"
```

### Strengths
- Zero extra build cost — observes builds that already happen
- Catches real production non-determinism (not just what static analysis predicts)
- Catches toolchain-level non-determinism that static analysis cannot
- Works retroactively — can backfill from existing BES data

### Weaknesses
- Reactive, not proactive — you detect after the non-determinism merges, not before
- Requires BES infrastructure (BuildBuddy or similar) to be already set up
- False positives: genuine cache invalidation (new feature, new dep) looks the same as non-determinism
- Needs signal calibration — how big a drop is meaningful?

### Implementation sketch
```python
# Query BuildBuddy API for cache hit rates per target
def get_target_hit_rates(project, last_n_builds=30):
    # BuildBuddy exposes per-invocation stats via their API
    # https://www.buildbuddy.io/docs/rbe-setup/
    ...

def detect_degradation(target, current_rate, historical_rates):
    avg = mean(historical_rates)
    if avg > 0.8 and current_rate < 0.3:
        return f"Hit rate for {target} dropped from {avg:.0%} to {current_rate:.0%}"
```

### Cost / signal

| Attribute | Value |
|-----------|-------|
| Cost | Zero — piggybacks on existing builds |
| Signal quality | Catches real non-determinism. Reactive (post-merge). Many false positives from legitimate changes. |
| Complexity | High — requires BES integration and statistical calibration |

---

## Alternative 3: Execution Log Diffing

**Idea**: Bazel's `--execution_log_json_file` records every action: its inputs, their hashes, the tool used, and its output hash. Compare execution logs from two separate builds (not the same CI run) with the same inputs. If an action has identical input hashes but different output hashes, it is non-deterministic.

### How it works

```bash
# Build 1 (main branch, commit A)
bazel build //... --execution_log_json_file=/tmp/run1.json

# Build 2 (same commit A, different machine or different day)
bazel build //... --execution_log_json_file=/tmp/run2.json

# Compare
python3 diff_exec_logs.py run1.json run2.json
# → finds actions where input_hash matches but output_hash differs
```

The execution log format (as JSON):
```json
{
  "runner": "linux-sandbox",
  "targetLabel": "//apps/cpp-lib:greeter",
  "mnemonic": "CppCompile",
  "inputs": [{"path": "apps/cpp-lib/greeter.cc", "digest": "abc123"}],
  "outputs": [{"path": "bazel-out/.../greeter.o", "digest": "def456"}]
}
```

### Strengths
- No extra builds in the same run — logs are cheap to collect
- Provides action-level granularity (which exact compile step is non-deterministic, not just which target)
- Can be diffed across machines to catch machine-specific non-determinism
- Works with any Bazel setup (no BES required)

### Weaknesses
- Requires storing and retrieving logs across separate builds (needs artifact storage)
- Execution log files can be large (100MB+ for big repos)
- Comparing logs from different builds is tricky: inputs must be identical (same commit, same toolchain)
- `--execution_log_json_file` has some performance overhead (~5% build time)

### Implementation sketch
```python
def diff_execution_logs(log1_path, log2_path):
    log1 = {a["targetLabel"]: a for a in load_log(log1_path)}
    log2 = {a["targetLabel"]: a for a in load_log(log2_path)}

    for label, action1 in log1.items():
        if label not in log2:
            continue
        action2 = log2[label]
        if action1["inputsDigest"] == action2["inputsDigest"]:
            if action1["outputsDigest"] != action2["outputsDigest"]:
                yield NonDeterministicAction(label, action1, action2)
```

### Cost / signal

| Attribute | Value |
|-----------|-------|
| Cost | ~5% build overhead for log collection. Zero extra builds. |
| Signal quality | High precision — input/output hash comparison at action level. Requires two separate builds with matching inputs. |
| Complexity | High — log storage, retrieval, and diffing infrastructure needed |

---

## Alternative 4: Sandbox Violation Detection

**Idea**: Bazel's sandbox can be configured to detect when an action reads files outside its declared `srcs` or `data` inputs. Undeclared reads are a major root cause of non-determinism (reading `/etc/hostname`, `/tmp/cache`, system libraries, etc.).

### How it works

```bazelrc
# .bazelrc
build --sandbox_debug
build --experimental_use_hermetic_linux_sandbox  # Linux only
build --incompatible_sandbox_hermetic_tmp         # isolates /tmp per action
```

With `--experimental_use_hermetic_linux_sandbox`, any action that reads an undeclared file gets a permission denied error at build time — it fails loudly rather than silently producing non-deterministic output.

For a lighter-weight version: `--sandbox_debug` logs all syscalls. A post-processor can scan the log for reads outside the declared input set.

### Strengths
- Catches the *root cause* directly — undeclared inputs are what cause non-determinism
- Zero extra builds — runs inline with normal builds
- Fails loudly (build error) rather than silently producing wrong output
- Works at the OS level — catches things static analysis cannot

### Weaknesses
- Hermetic sandbox is Linux-only (no macOS support for full hermeticity)
- Some false positives: `/proc`, `/sys`, `/dev/urandom` are commonly read and usually benign
- Doesn't catch non-determinism from within-sandbox sources (e.g. maps without sorted iteration, random seeds)
- `--sandbox_debug` produces very verbose output (gigabytes on large builds)

### When to use
Best as a one-time audit tool or in local development, not continuous CI. Run on a Linux machine with:
```bash
bazel build //... --experimental_use_hermetic_linux_sandbox
```
Any build error = undeclared dependency = likely source of non-determinism.

### Cost / signal

| Attribute | Value |
|-----------|-------|
| Cost | Low on Linux (hermetic sandbox runs inline). Not available on macOS. |
| Signal quality | High for undeclared-input non-determinism. Misses within-sandbox issues. |
| Complexity | Low (one flag) — but Linux-only limits usefulness for Mac-primary teams |

---

## Alternative 5: Static Analysis (expanded)

**Idea**: Expand the existing 16-pattern static checker to cover more cases. No builds needed at all.

### Patterns not yet covered

| Pattern | Language | Risk |
|---------|----------|------|
| `genrule(` without `tools=` | BUILD | HIGH — tool from PATH, version may differ |
| `map[string]...` iteration in test output | Go | MEDIUM — map iteration order is random |
| `os.listdir()` without `sorted()` | Python | MEDIUM — filesystem order differs by OS |
| `set()` iteration in output | Python | MEDIUM — set order is non-deterministic |
| `Arrays.asList()` → iteration in output | Java | LOW — list order from reflection can vary |
| `GOFLAGS` / `CGO_ENABLED` in env | BUILD | LOW — environment leaks into Go binary |
| Absolute paths in `cmd=` | BUILD | MEDIUM — path differs between machines |

### Strengths
- Fastest possible (no builds, pure text scan)
- Runs in ~2s on any repo size
- Easy to add new patterns

### Weaknesses
- Cannot catch what it doesn't pattern-match
- Cannot catch toolchain-level non-determinism (compiler internals, linker, etc.)
- High false-positive risk if patterns are too broad

### Cost / signal

| Attribute | Value |
|-----------|-------|
| Cost | Near-zero |
| Signal quality | Low-medium — incomplete by design. Useful as a first filter, not a guarantee. |
| Complexity | Low |

---

## Comparison Summary

| Approach | Build cost | Catches toolchain issues | Proactive (pre-merge)? | Complexity |
|----------|-----------|--------------------------|------------------------|------------|
| Build twice (current) | 2× per PR | Yes | Yes | Low |
| Golden hash baseline | 1× per PR + nightly | No | Partially | Medium |
| Cache miss monitoring | Zero | Yes | No (reactive) | High |
| Execution log diffing | ~5% overhead | Yes | Partially | High |
| Sandbox violation | Zero (Linux only) | Partial | Yes | Low |
| Static analysis (expanded) | Zero | No | Yes | Low |

---

## Recommended Architecture for v0.2

Based on the tradeoffs above, a practical combination:

| Layer | Trigger | Approach | Goal |
|-------|---------|----------|------|
| 1 | Every PR | Static analysis (expanded patterns) | Block obvious mistakes fast |
| 2 | Every PR | Golden hash baseline (unchanged targets) | Catch persistent non-determinism cheaply |
| 3 | Nightly on `main` | Build twice | Definitive proof for the full target graph |
| 4 | Optional | Cache miss monitoring | Long-term observability |

This means:
- PRs stay fast (no double builds)
- Non-determinism introduced in previous PRs gets caught when a new PR doesn't touch those targets
- The nightly job provides the definitive guarantee
- Teams with BES already set up get observability for free

---

## Open Questions

1. Where to store the golden hash baseline? Options: GCS, GitHub Actions artifact (90-day retention), or committed to the repo (noisy diffs).
2. How to handle toolchain version bumps that legitimately change all hashes? Need a "reset golden" workflow.
3. Should v0.2 be a Bazel rule, a GitHub Actions workflow, or both?
4. John's experiment (Bazel #rules, June 2026): run `determinism_test()` against popular rulesets' own test targets. Would surface real toolchain-level non-determinism in the wild. Worth prototyping.

---

*Document created June 2026. Feedback: Corentin Kerisit, John Cater (katre), Brian Chesko — Bazel #rules Slack.*
