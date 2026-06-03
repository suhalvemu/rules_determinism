load("@rules_python//python:defs.bzl", "py_binary")
load("@rules_shell//shell:sh_binary.bzl", "sh_binary")

py_binary(
    name = "static_check",
    srcs = ["static_check.py"],
    main = "static_check.py",
    visibility = ["//:__subpackages__"],
)

py_binary(
    name = "summarize",
    srcs = ["summarize.py"],
    main = "summarize.py",
    visibility = ["//:__subpackages__"],
)

sh_binary(
    name = "dynamic_check",
    srcs = ["dynamic_check.sh"],
    visibility = ["//:__subpackages__"],
)

exports_files([
    "rules.bzl",
    "static_check.py",
    "summarize.py",
    "dynamic_check.sh",
])
