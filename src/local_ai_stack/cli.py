"""The single entrypoint: ``local-ai-stack`` on a checkout, ``PepitosAIHub.exe`` when built.

Both spellings run :func:`main`. With no flag it opens the Hub. ``--doctor``
prints the environment report and ``--smoke`` proves a build can stand up -
application importable, resources present, state path resolvable - and exits
without opening a window. ``hub`` operates the same Hub without the window,
in JSON, for coding agents (docs/AGENT_WORKFLOW.md). None of them is a second
application flow; they are the same program answering a question instead of
opening a window.
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
    auth = parser.add_mutually_exclusive_group()
    auth.add_argument(
        "--hf-login",
        action="store_true",
        help="authorize gated Hugging Face model downloads",
    )
    auth.add_argument(
        "--hf-status",
        action="store_true",
        help="verify the saved Hugging Face credential",
    )
    auth.add_argument(
        "--hf-logout",
        action="store_true",
        help="remove the locally stored Hugging Face credential",
    )
    parser.add_argument("--version", action="version", version=f"{PRODUCT_NAME} {__version__}")
    areas = parser.add_subparsers(dest="area", metavar="hub")
    hub = areas.add_parser("hub", help="operate the Hub without the window, in JSON (for agents)")
    hub_commands = hub.add_subparsers(dest="hub_command", required=True)
    state = hub_commands.add_parser("state", help="print the Hub state as JSON")
    state.add_argument("--json", action="store_true", help="JSON output (the default)")
    hub_commands.add_parser("commands", help="list the commands `hub run` accepts")
    run = hub_commands.add_parser("run", help="run one allowlisted command")
    run.add_argument("name", metavar="command")
    run.add_argument("--args", dest="command_args", metavar="JSON", help="JSON object of args")
    run.add_argument(
        "--wait", action="store_true", help="stream the operation's events until it ends"
    )
    run.add_argument("--timeout", type=_seconds, metavar="S", help="with --wait: stop after S")
    serve = hub_commands.add_parser("serve", help="serve the UI on loopback and print its URL")
    serve.add_argument("--no-open", action="store_true", help="do not launch a browser")
    export = hub_commands.add_parser(
        "export-session", help="export owned session evidence without credentials"
    )
    export.add_argument("--root", required=True, help="absolute installation root")
    export.add_argument("--output", required=True, help="destination ZIP file")
    fetch = hub_commands.add_parser(
        "fetch-session", help="collect through SSH and verify a local copy"
    )
    fetch.add_argument("--host", required=True, help="verified OpenSSH host or alias")
    fetch.add_argument("--port", type=int, default=22)
    fetch.add_argument("--identity-file")
    fetch.add_argument("--bundle", required=True, help="absolute remote source bundle directory")
    fetch.add_argument("--root", required=True, help="absolute remote installation root")
    fetch.add_argument("--output", required=True, help="new local ZIP file")
    args = parser.parse_args(argv)
    if args.area == "hub" and args.hub_command == "run" and args.timeout and not args.wait:
        run.error("--timeout requires --wait")
    if args.doctor:
        from .doctor import doctor_text

        print(doctor_text())
        return 0
    if args.smoke:
        report = smoke_report()
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0 if report["ok"] else 1
    if args.hf_login or args.hf_status or args.hf_logout:
        from .huggingface_auth import (
            HuggingFaceAuthError,
            check_status,
            clear_token,
            interactive_login,
        )

        try:
            if args.hf_logout:
                clear_token()
                print("Hugging Face credential removed.")
                return 0
            status = interactive_login() if args.hf_login else check_status()
        except HuggingFaceAuthError as exc:
            print(f"Hugging Face: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(status.as_dict(), indent=2, ensure_ascii=False))
        return 0 if status.authenticated else 1
    if args.area == "hub":
        if args.hub_command == "export-session":
            import hashlib
            from pathlib import Path

            from .layout import StackLayout
            from .session_export import SessionExportError, export_session

            try:
                output = Path(args.output).expanduser().absolute()
                output.parent.mkdir(parents=True, exist_ok=True)
                archive = export_session(StackLayout.from_root(args.root), output)
                with archive.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                print(
                    json.dumps(
                        {"archive": str(archive), "sha256": digest, "bytes": archive.stat().st_size}
                    )
                )
                return 0
            except (SessionExportError, OSError, ValueError) as exc:
                from .runner import sanitize

                print(json.dumps({"ok": False, "error": sanitize(str(exc))}))
                return 1
        if args.hub_command == "fetch-session":
            from pathlib import Path

            from .session_transfer import fetch_session

            try:
                archive = fetch_session(
                    host=args.host,
                    port=args.port,
                    identity_file=args.identity_file,
                    bundle=args.bundle,
                    root=args.root,
                    destination=Path(args.output),
                )
                print(json.dumps({"ok": True, "archive": str(archive)}))
                return 0
            except (OSError, ValueError, RuntimeError) as exc:
                from .runner import sanitize

                print(json.dumps({"ok": False, "error": sanitize(str(exc))}))
                return 1
        from .desktop import agent

        if args.hub_command == "state":
            return agent.hub_state()
        if args.hub_command == "commands":
            return agent.hub_commands()
        if args.hub_command == "run":
            return agent.hub_run(args.name, args.command_args, wait=args.wait, timeout=args.timeout)
        return agent.hub_serve(open_browser=not args.no_open)
    from .desktop import run_desktop

    return run_desktop()


def _seconds(value: str) -> float:
    seconds = float(value)
    if not seconds > 0:
        raise argparse.ArgumentTypeError("must be a positive number of seconds")
    return seconds


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

    def image_filter() -> dict:
        # The Filter Function the Hub installs into Open WebUI is a resource;
        # a build that cannot read it cannot connect the Image feature.
        from .open_webui_filters import FILTER_ID, filter_path, filter_source

        source = filter_source()
        if "required_open_webui_version:" not in source or "class Filter" not in source:
            raise RuntimeError(f"{filter_path()} is not the image chat filter")
        return {"id": FILTER_ID, "path": str(filter_path()), "bytes": len(source)}

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
        from .desktop.server import DesktopServer
        from .hub_application import HubApplicationAdapter

        return f"{DesktopServer.__name__}, {HubApplicationAdapter.__name__}"

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

    def desktop_assets() -> dict:
        directory = resource_root() / "desktop-ui" / "dist"
        index = directory / "index.html"
        if not index.is_file():
            raise RuntimeError("desktop assets missing; run npm ci && npm run build in desktop-ui")
        return {"index": str(index)}

    check("desktop", desktop_assets)

    check("profiles", profiles)
    check("manifests", manifests)
    check("workflows", workflows)
    check("image_filter", image_filter)
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
