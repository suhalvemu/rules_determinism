"""
rules_determinism — Bazel rules that catch non-deterministic build actions
at PR time, protecting remote cache hit rates before they silently degrade.

Usage:
    load("@rules_determinism//:defs.bzl", "determinism_test")

    determinism_test(
        name = "my_target_is_deterministic",
        targets = [
            "//apps/go-service:go-service",
            "//apps/cpp-lib:greeter",
        ],
    )
"""

load("//internal:rules.bzl", _determinism_test = "determinism_test")

determinism_test = _determinism_test
