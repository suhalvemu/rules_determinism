"""Public API for rules_determinism.

Usage:
    load("@rules_determinism//determinism:defs.bzl", "determinism_test")

    determinism_test(
        name = "my_target_is_deterministic",
        targets = [
            "//apps/go-service:go-service",
            "//apps/cpp-lib:greeter",
        ],
    )

Then run:
    bazel test //path/to:my_target_is_deterministic
"""

load("//determinism/private:rules.bzl", "determinism_test")

# Re-export as public API
determinism_test = determinism_test
