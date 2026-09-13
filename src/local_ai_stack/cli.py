"""The single entrypoint: ``local-ai-stack`` on a checkout, ``PepitosAIHub.exe`` when built.

Both spellings run :func:`main`. With no flag it opens the Hub. ``--doctor``
prints the environment report and ``--smoke`` proves a build can stand up -
application importable, resources present, state path resolvable - and exits
without ever drawing the TUI. Neither is a second application flow; they are
the same program answering a question instead of opening a window.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import PRODUCT_NAME, __version__, display_version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="local-ai-stack")
    parser.add_argument("--doctor", action="store_true", help="print the environment report")
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="check that this build finds its application, resources and state path",
    )
    parser.add_argument("--version", action="version", version=f"{PRODUCT_NAME} {__version__}")
    args = parser.parse_args(argv)
    if args.doctor:
        from .doctor import doctor_text

        print(doctor_text())
        return 0
    if args.smoke:
        report = smoke_report()
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0 if report["ok"] else 1
    from .app import run_tui

    run_tui()
    return 0


def smoke_report() -> dict:
    """What a packaged smoke test needs to know, as data. Never raises.

    Every check is read-only. A failure lands in ``problems`` with the check's
    name, so a build that cannot find its manifests says "manifests", not
    "Traceback".
    """
    from .resources import (
        RESOURCE_DIRECTORIES,
        bundle_directory,
        executable_directory,
        is_frozen,
        missing_resources,
        resource_root,
    )

    problems: list[str] = []
    report: dict = {
        "product": PRODUCT_NAME,
        "version": __version__,
        "display_version": display_version(),
        "frozen": is_frozen(),
        "executable": sys.executable,
        "executable_directory": str(executable_directory()),
        "bundle_directory": str(bundle_directory()) if bundle_directory() else None,
        "resource_root": str(resource_root()),
        "resources": {},
        "missing_resources": list(missing_resources()),
    }
    for name in RESOURCE_DIRECTORIES:
        report["resources"][name] = str(resource_root() / name)
    if report["missing_resources"]:
        problems.append("resources: " + ", ".join(report["missing_resources"]))

    def check(name: str, probe) -> None:
        try:
            report[name] = probe()
        except Exception as exc:  # noqa: BLE001 -- the report must reach the caller
            problems.append(f"{name}: {type(exc).__name__}: {exc}")

    def profiles() -> list[str]:
        from .profiles import load_profiles

        loaded = load_profiles(resource_root() / "profiles")
        if not loaded:
            raise RuntimeError("no profiles loaded")
        return [profile.id for profile in loaded]

    def manifests() -> dict:
        from .catalog import artifact_catalog, package_catalog, repository_catalog

        artifacts = artifact_catalog()
        if not artifacts:
            raise RuntimeError("artifact catalog is empty")
        return {
            "artifacts": sorted(artifacts),
            "repositories": sorted(repository_catalog()),
            "packages": sorted(package_catalog()),
        }

    def workflows() -> dict:
        from .workflows import load_workflow_manifest

        specs = load_workflow_manifest(resource_root() / "manifests" / "workflows.yaml")
        # Only a workflow the manifest calls embedded must ship its graph; a
        # TODO entry is a declared gap, not a packaging error.
        embedded = {spec.id: str(spec.file) for spec in specs.values() if "embedded" in spec.status}
        missing = sorted(
            spec_id for spec_id, file in embedded.items() if not (resource_root() / file).is_file()
        )
        if missing:
            raise RuntimeError(f"embedded workflow graphs missing: {', '.join(missing)}")
        return {"declared": sorted(specs), "embedded": sorted(embedded)}

    def state_path() -> str:
        from .installation import InstallLocation, resolve_installation

        resolved = resolve_installation(InstallLocation.default())
        return str(resolved.layout.state)

    def portable_root() -> str:
        from .installation import InstallLocation, resolve_install_root

        return str(resolve_install_root(InstallLocation.portable()))

    def python_runtime() -> dict:
        from .catalog import artifact_for
        from .pyenv import running_interpreter_is_usable
        from .python_runtime import COMPONENT_ID, managed_python_runtime

        artifact = artifact_for(COMPONENT_ID)
        if artifact is None:
            raise RuntimeError("no managed Python artifact is pinned")
        runtime = managed_python_runtime(artifact)
        return {
            "pinned": artifact.version,
            "managed_runtime_selected": runtime is not None,
            "running_interpreter_usable": running_interpreter_is_usable(),
        }

    def folder_picker() -> str:
        from .folder_picker import default_folder_picker

        return type(default_folder_picker()).__name__

    def application() -> str:
        from .app import LocalAIStackApp
        from .hub_application import HubApplicationAdapter

        return f"{LocalAIStackApp.__name__}, {HubApplicationAdapter.__name__}"

    def composition() -> dict:
        """Build what the Hub builds to open, against the default root.

        This is the path the first packaged build crashed on: the install
        actions default their data root, the plan panel composes an executor
        from it, and the hub application resolves every location. Nothing
        here writes, downloads or starts anything -- the executor only
        resolves plans, exactly as the Installation section does on render.
        """
        from .app import default_executor_factory, project_root, workspace_root
        from .hub_application import create_hub_application
        from .installation import InstallLocation, resolve_installation
        from .integration import default_install_actions, provision_context
        from .planning import build_install_plan

        installation = resolve_installation(InstallLocation.default())
        actions = default_install_actions(provision_context(installation))
        if actions.repo_root != resource_root():
            raise RuntimeError(f"install actions read {actions.repo_root}, not the resource root")
        executor = default_executor_factory(installation)
        # A step the executor cannot run (a "check" it only renders) is not a
        # packaging problem; a resource it cannot read raises, and that is.
        plans = {}
        for profile in actions.profiles.values():
            steps = executor.resolve(build_install_plan(profile, installation=installation))
            plans[profile.id] = {
                "steps": len(steps),
                "executable": sum(1 for item in steps if item.supported),
            }
        hub = create_hub_application(project_root())
        try:
            portable = hub.resolve(InstallLocation.portable()).root
        finally:
            hub.close()
        if portable != workspace_root() / "stack":
            raise RuntimeError(f"portable resolves to {portable}, not beside the workspace")
        return {
            "resource_root": str(actions.repo_root),
            "workflow_specs": sorted(actions.workflow_specs),
            "plans": plans,
            "portable_root": str(portable),
        }

    check("profiles", profiles)
    check("manifests", manifests)
    check("workflows", workflows)
    check("state_path", state_path)
    check("portable_root", portable_root)
    check("python_runtime", python_runtime)
    check("folder_picker", folder_picker)
    check("application", application)
    check("composition", composition)

    if is_frozen():
        # A frozen build must never plan to build a venv from itself.
        if report.get("python_runtime", {}).get("running_interpreter_usable"):
            problems.append("python_runtime: frozen process still offers sys.executable")
        if not report.get("python_runtime", {}).get("managed_runtime_selected"):
            problems.append("python_runtime: frozen process did not select the managed runtime")
        bundle = bundle_directory()
        composed = report.get("composition") or {}
        for name, value in (
            ("state_path", report.get("state_path")),
            ("portable_root", report.get("portable_root")),
            ("composition.portable_root", composed.get("portable_root")),
        ):
            if bundle is not None and value and str(value).startswith(str(bundle)):
                problems.append(f"{name}: inside the bundle ({value})")

    report["problems"] = problems
    report["ok"] = not problems
    return report


if __name__ == "__main__":
    sys.exit(main())
