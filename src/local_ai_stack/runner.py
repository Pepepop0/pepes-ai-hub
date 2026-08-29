"""Safe subprocess wrapper.

Regras do projeto:
- nunca `shell=True`;
- argv sempre como lista de strings;
- timeout obrigatorio (default configuravel);
- stdout/stderr capturados e exit code preservado;
- `dry_run` nao executa nada;
- logs sempre sanitizados (tokens, senhas e credenciais em URL).

O runner nunca altera `os.environ` nem o PATH global: variaveis extras sao
aplicadas sobre uma copia do ambiente atual, valida apenas para o processo filho.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from os import PathLike
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0
RETURNCODE_TIMEOUT = -1
RETURNCODE_NOT_FOUND = 127

REDACTED = "***"

_SECRET_WORDS = (
    r"tokens?|api[-_]?keys?|secrets?|passwords?|passwd|pwd|access[-_]?tokens?|"
    r"auth|credentials?|bearer"
)

# https://user:pass@host -> https://***:***@host
_URL_CRED_RE = re.compile(r"(?P<scheme>[a-zA-Z][\w+.\-]*://)(?P<user>[^/\s:@]+)(?::[^/\s@]*)?@")
# ?token=abc / &api_key=abc
_QUERY_RE = re.compile(rf"(?i)([?&](?:{_SECRET_WORDS})=)[^&\s]+")
# --token=abc / --api-key=abc
_FLAG_EQ_RE = re.compile(rf"(?i)(--(?:{_SECRET_WORDS})[\w-]*=)\S+")
# Authorization: abc / X-Api-Key: abc
_HEADER_RE = re.compile(r"(?i)\b(authorization|x-api-key)(\s*:\s*)\S+")
# Bearer abc
_BEARER_RE = re.compile(r"(?i)\b(bearer)\s+\S+")
# tokens com prefixo conhecido, soltos em qualquer texto
_TOKEN_LITERAL_RE = re.compile(
    r"\b(?:hf_[A-Za-z0-9]{16,}|gh[pousr]_[A-Za-z0-9]{16,}|sk-[A-Za-z0-9_\-]{16,})"
)
# flag secreta cujo valor vem no argv seguinte: --token abc
_SECRET_FLAG_RE = re.compile(rf"(?i)^--?(?:{_SECRET_WORDS})[\w-]*$")


def sanitize(text: str) -> str:
    """Remove credenciais de um texto antes de logar ou exibir."""
    if not text:
        return text
    out = _URL_CRED_RE.sub(rf"\g<scheme>{REDACTED}:{REDACTED}@", text)
    out = _QUERY_RE.sub(rf"\1{REDACTED}", out)
    out = _FLAG_EQ_RE.sub(rf"\1{REDACTED}", out)
    out = _HEADER_RE.sub(rf"\1\2{REDACTED}", out)
    out = _BEARER_RE.sub(rf"\1 {REDACTED}", out)
    return _TOKEN_LITERAL_RE.sub(REDACTED, out)


def sanitize_argv(argv: Sequence[str]) -> tuple[str, ...]:
    """Sanitiza cada item do argv, inclusive o valor que segue uma flag secreta."""
    out: list[str] = []
    redact_next = False
    for item in argv:
        if redact_next:
            out.append(REDACTED)
            redact_next = False
            continue
        out.append(sanitize(item))
        redact_next = bool(_SECRET_FLAG_RE.match(item))
    return tuple(out)


class CommandError(RuntimeError):
    """Erro base do runner."""

    def __init__(self, message: str, result: CommandResult | None = None) -> None:
        super().__init__(message)
        self.result = result


class CommandNotFoundError(CommandError):
    """Executavel nao encontrado no PATH do processo filho."""


class CommandTimeoutError(CommandError):
    """Comando excedeu o timeout e foi encerrado."""


class CommandFailedError(CommandError):
    """Comando terminou com exit code diferente de zero (apenas com check=True)."""


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""
    duration_s: float = 0.0
    timed_out: bool = False
    dry_run: bool = False
    not_found: bool = False
    cwd: str | None = None
    safe_argv: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.not_found

    @property
    def display(self) -> str:
        """Linha de comando ja sanitizada, segura para log e TUI."""
        return " ".join(self.safe_argv or sanitize_argv(self.argv))

    def __str__(self) -> str:
        state = "dry-run" if self.dry_run else ("timeout" if self.timed_out else self.returncode)
        return f"<CommandResult {self.display} -> {state}>"


def _as_argv(argv: Sequence[str | PathLike[str]]) -> tuple[str, ...]:
    if isinstance(argv, (str, bytes, PathLike)):
        raise TypeError(
            "argv deve ser uma lista/tupla de argumentos, nao uma string. "
            "O runner nunca usa shell=True, entao strings de comando nao sao suportadas."
        )
    if not isinstance(argv, Sequence):
        raise TypeError(f"argv deve ser uma sequencia, recebido {type(argv).__name__}")
    items = [os.fspath(a) if isinstance(a, PathLike) else a for a in argv]
    if not items:
        raise ValueError("argv vazio")
    for item in items:
        if not isinstance(item, str):
            raise TypeError(f"argv contem item nao-string: {type(item).__name__}")
    return tuple(items)


def _decode(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _platform_kwargs() -> dict[str, int]:
    """Evita abrir janela de console no Windows (importante sob a TUI)."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", None)
    return {"creationflags": flags} if flags is not None and os.name == "nt" else {}


class CommandRunner:
    """Executa comandos externos de forma previsivel, testavel e sem shell."""

    def __init__(
        self,
        *,
        dry_run: bool = False,
        default_timeout: float = DEFAULT_TIMEOUT,
        cwd: str | PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
        log: logging.Logger | None = None,
    ) -> None:
        if default_timeout <= 0:
            raise ValueError("default_timeout deve ser > 0")
        self.dry_run = dry_run
        self.default_timeout = default_timeout
        self.cwd = Path(cwd) if cwd is not None else None
        self.env_overrides = dict(env) if env else {}
        self.log = log or logger

    def child_env(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        """Ambiente do processo filho. Nunca modifica os.environ."""
        merged = dict(os.environ)
        merged.update(self.env_overrides)
        if extra:
            merged.update(extra)
        return merged

    def which(self, name: str, extra_env: Mapping[str, str] | None = None) -> str | None:
        """Resolve um executavel usando o PATH do processo filho."""
        return shutil.which(name, path=self.child_env(extra_env).get("PATH"))

    def available(self, name: str) -> bool:
        return self.which(name) is not None

    def run(
        self,
        argv: Sequence[str | PathLike[str]],
        *,
        timeout: float | None = None,
        cwd: str | PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
        check: bool = False,
        input_text: str | None = None,
        dry_run: bool | None = None,
    ) -> CommandResult:
        """Roda argv e devolve um CommandResult.

        Nao levanta excecao por exit code diferente de zero a menos que check=True.
        """
        items = _as_argv(argv)
        safe = sanitize_argv(items)
        effective_timeout = self.default_timeout if timeout is None else timeout
        if effective_timeout <= 0:
            raise ValueError("timeout deve ser > 0")
        effective_cwd = Path(cwd) if cwd is not None else self.cwd
        cwd_str = str(effective_cwd) if effective_cwd is not None else None
        is_dry = self.dry_run if dry_run is None else dry_run

        if is_dry:
            self.log.info("[dry-run] %s", " ".join(safe))
            return CommandResult(
                argv=items,
                returncode=0,
                duration_s=0.0,
                dry_run=True,
                cwd=cwd_str,
                safe_argv=safe,
            )

        self.log.debug(
            "run: %s (cwd=%s, timeout=%.1fs)", " ".join(safe), cwd_str, effective_timeout
        )
        start = time.monotonic()
        try:
            completed = subprocess.run(
                items,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=effective_timeout,
                cwd=cwd_str,
                env=self.child_env(env),
                input=input_text,
                check=False,
                shell=False,
                **_platform_kwargs(),
            )
        except subprocess.TimeoutExpired as exc:
            result = CommandResult(
                argv=items,
                returncode=RETURNCODE_TIMEOUT,
                stdout=_decode(exc.stdout),
                stderr=_decode(exc.stderr),
                duration_s=time.monotonic() - start,
                timed_out=True,
                cwd=cwd_str,
                safe_argv=safe,
            )
            self.log.warning("timeout apos %.1fs: %s", effective_timeout, " ".join(safe))
            if check:
                raise CommandTimeoutError(
                    f"timeout apos {effective_timeout:.1f}s: {result.display}", result
                ) from exc
            return result
        except (FileNotFoundError, NotADirectoryError) as exc:
            result = CommandResult(
                argv=items,
                returncode=RETURNCODE_NOT_FOUND,
                stderr=f"command not found: {safe[0]}",
                duration_s=time.monotonic() - start,
                not_found=True,
                cwd=cwd_str,
                safe_argv=safe,
            )
            self.log.warning("executavel nao encontrado: %s", safe[0])
            if check:
                raise CommandNotFoundError(f"executavel nao encontrado: {safe[0]}", result) from exc
            return result
        except OSError as exc:
            detail = sanitize(str(exc))
            result = CommandResult(
                argv=items,
                returncode=RETURNCODE_NOT_FOUND,
                stderr=detail,
                duration_s=time.monotonic() - start,
                not_found=True,
                cwd=cwd_str,
                safe_argv=safe,
            )
            self.log.warning("falha ao iniciar %s: %s", safe[0], detail)
            if check:
                raise CommandNotFoundError(f"falha ao iniciar {safe[0]}: {detail}", result) from exc
            return result

        result = CommandResult(
            argv=items,
            returncode=completed.returncode,
            stdout=_decode(completed.stdout),
            stderr=_decode(completed.stderr),
            duration_s=time.monotonic() - start,
            cwd=cwd_str,
            safe_argv=safe,
        )
        self.log.debug("exit=%s em %.2fs: %s", result.returncode, result.duration_s, result.display)
        if check and not result.ok:
            raise CommandFailedError(
                f"exit {result.returncode}: {result.display}\n{sanitize(result.stderr).strip()}",
                result,
            )
        return result
