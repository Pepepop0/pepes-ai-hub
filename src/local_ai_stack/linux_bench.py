"""Explicit Linux GPU bench bootstrap; installation remains the existing Hub operation."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import platform
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

import psutil

from .catalog import repository_for
from .compatibility import evaluate_compatibility
from .layout import ENV_ROOT, StackLayout
from .model_resolver import profile_model_specs
from .models import FileModelSource
from .profiles import Profile, load_profiles
from .resources import resource_root
from .system import SystemFacts, detect_system

DEFAULT_PROFILE = "capella"
CIVITAI_HOSTS = frozenset({"civitai.com", "www.civitai.com"})


class BenchProfileError(ValueError):
    """The requested profile does not exist in this bundle."""


def bench_profile(profile_id: str = DEFAULT_PROFILE) -> Profile:
    """The shipped profile (with any pack overlay laid over it) the bench installs."""
    profiles = {profile.id: profile for profile in load_profiles(resource_root() / "profiles")}
    if profile_id not in profiles:
        raise BenchProfileError(
            f"Unknown profile {profile_id!r}; this bundle has: {', '.join(sorted(profiles))}."
        )
    return profiles[profile_id]


def needs_civitai(profile: Profile) -> bool:
    """True when any model of the profile is downloaded from Civitai (token required)."""
    return any(
        isinstance(spec.source, FileModelSource)
        and (urlsplit(spec.source.artifact.url).hostname or "").lower() in CIVITAI_HOSTS
        for spec in profile_model_specs(profile)
    )


def bench_preflight(
    layout: StackLayout, facts: SystemFacts | None = None, profile_id: str = DEFAULT_PROFILE
) -> dict:
    """Read-only: no token lookup, downloads, configuration or model loading."""
    facts = facts or detect_system()
    profile = bench_profile(profile_id)
    compatibility = evaluate_compatibility(facts, profile)
    issues = [item.message for item in compatibility.blockers]
    if facts.os_name.casefold() != "linux":
        issues.append("The remote bench requires Linux; the Windows Hub remains unchanged.")
    if (facts.machine or platform.machine()).casefold() not in {"x86_64", "amd64"}:
        issues.append("The pinned Linux GPU stack requires an x86-64 machine.")
    try:
        python_version = tuple(int(x) for x in facts.python_version.split(".")[:2])
    except ValueError:
        python_version = ()
    if not (3, 11) <= python_version < (3, 13):
        issues.append("Use Python 3.11 or 3.12 for the pinned Open WebUI and ComfyUI.")
    pin = repository_for("comfyui")
    requirements = pin.dependencies.requirements if pin and pin.dependencies else ()
    cuda = next(
        (
            re.search(r"\+cu(\d{2,3})$", value)
            for value in requirements
            if value.startswith("torch==")
        ),
        None,
    )
    if cuda:
        digits = cuda.group(1)
        required = (int(digits[:-1]), int(digits[-1]))
        supported = facts.nvidia.cuda_version_supported_by_driver
        try:
            actual = tuple(int(part) for part in (supported or "").split(".")[:2])
        except ValueError:
            actual = ()
        if len(actual) != 2 or actual < required:
            issues.append(
                f"The pinned torch build needs driver CUDA support >= {required[0]}.{required[1]}; "
                "choose a compatible host, not an in-container driver replacement."
            )
    if psutil.virtual_memory().total < 30 * 1024**3:
        issues.append("At least 32 GB physical system RAM is required; 64 GB is recommended.")
    if layout.root == Path(layout.root.anchor) or layout.root == Path.home():
        issues.append("Choose a dedicated installation subdirectory, not / or your home itself.")
    return {
        "ok": not issues,
        "profile": profile.id,
        "root": str(layout.root),
        "os": facts.os_name,
        "gpus": [{"name": gpu.name, "vram_mib": gpu.vram_total_mib} for gpu in facts.gpus],
        "issues": issues,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "install"))
    parser.add_argument("--root", required=True)
    parser.add_argument(
        "--profile", default=DEFAULT_PROFILE, help=f"profile to install (default {DEFAULT_PROFILE})"
    )
    args = parser.parse_args(argv)
    layout = StackLayout.from_root(args.root)
    try:
        report = bench_preflight(layout, profile_id=args.profile)
        civitai_needed = needs_civitai(bench_profile(args.profile))
    except BenchProfileError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=True), flush=True)
    if not report["ok"] or args.action == "check":
        return 0 if report["ok"] else 1

    # The token is neither a command argument nor a plaintext credential file.
    # Existing request_headers() supplies it only to authenticated Civitai HTTPS downloads,
    # and only profiles with Civitai models ask for it.
    previous_root = os.environ.get(ENV_ROOT)
    previous_token = os.environ.get("CIVITAI_TOKEN")
    try:
        os.environ[ENV_ROOT] = str(layout.root)
        if civitai_needed and not (previous_token or "").strip():
            if not sys.stdin.isatty():
                print(
                    "Provide CIVITAI_TOKEN through the remote secret environment or run in a terminal.",
                    file=sys.stderr,
                )
                return 1
            token = getpass.getpass("Civitai token (hidden; used only for this install): ").strip()
            if not token:
                print(
                    f"Civitai token is required for the declared {args.profile} models.",
                    file=sys.stderr,
                )
                return 1
            os.environ["CIVITAI_TOKEN"] = token
        from .cli import main as hub_main

        return hub_main(
            [
                "hub",
                "run",
                "install",
                "--args",
                json.dumps({"profile_id": args.profile, "mode": "default"}),
                "--wait",
            ]
        )
    finally:
        for key, previous in ((ENV_ROOT, previous_root), ("CIVITAI_TOKEN", previous_token)):
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous


if __name__ == "__main__":
    raise SystemExit(main())
