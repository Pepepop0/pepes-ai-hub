"""Machine prerequisites the Hub may install only after explicit user consent."""

from __future__ import annotations

from dataclasses import dataclass

from .runner import CommandRunner

GIT_DOWNLOAD_URL = "https://git-scm.com/download/win"
GIT_PACKAGE_ID = "Git.Git"
WINGET = "winget"
INSTALL_TIMEOUT_SECONDS = 20 * 60


class PrerequisiteInstallError(RuntimeError):
    """A prerequisite could not be installed safely through the approved provider."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class GitInstallResult:
    installed: bool
    changed: bool
    restart_required: bool
    message: str


def install_git_with_winget(runner: CommandRunner | None = None) -> GitInstallResult:
    """Install the fixed Git for Windows package through Windows Package Manager.

    No renderer-controlled package id, URL, command, or flags cross this boundary.
    A successful install still requires reopening the Hub because this process keeps
    the PATH inherited at launch and compatibility facts are cached for the session.
    """
    command_runner = runner or CommandRunner()
    if command_runner.which("git") is not None:
        return GitInstallResult(True, False, False, "O Git já está disponível neste sistema.")

    winget = command_runner.which(WINGET)
    if winget is None:
        raise PrerequisiteInstallError(
            "O Windows Package Manager (winget) não está disponível. "
            "Use Baixar manualmente para instalar o Git for Windows.",
            "winget_missing",
        )

    result = command_runner.run(
        [
            winget,
            "install",
            "--id",
            GIT_PACKAGE_ID,
            "--exact",
            "--source",
            "winget",
            "--accept-package-agreements",
            "--accept-source-agreements",
            "--silent",
            "--disable-interactivity",
        ],
        timeout=INSTALL_TIMEOUT_SECONDS,
        dry_run=False,
    )
    if result.timed_out:
        raise PrerequisiteInstallError(
            "A instalação do Git excedeu 20 minutos. Verifique o instalador do Windows "
            "antes de tentar novamente.",
            "winget_timeout",
        )
    if result.not_found:
        raise PrerequisiteInstallError(
            "O winget deixou de estar disponível. Use Baixar manualmente para instalar o Git.",
            "winget_missing",
        )
    if not result.ok:
        raise PrerequisiteInstallError(
            f"O winget não concluiu a instalação do Git (código {result.returncode}). "
            "Use Baixar manualmente ou tente novamente.",
            "winget_failed",
        )
    return GitInstallResult(
        True,
        True,
        True,
        "Git instalado. Feche e abra o Hub para atualizar os requisitos do sistema.",
    )
