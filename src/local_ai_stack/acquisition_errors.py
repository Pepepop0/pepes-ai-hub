"""Allowlisted model-download causes and guidance, never raw backend error text."""

from __future__ import annotations

import errno
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

import httpx

from .download import ChecksumMismatchError, DownloadError


class DownloadProvider(StrEnum):
    CIVITAI = "civitai"
    HUGGING_FACE = "huggingface"


@dataclass(frozen=True)
class AcquisitionFailure:
    code: str
    message: str
    provider: DownloadProvider | None = None
    http_status: int | None = None
    error_type: str | None = None
    os_errno: int | None = None
    os_winerror: int | None = None
    partials_retained: bool = False


def _provider(url: str) -> DownloadProvider | None:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return None
    if host in {"civitai.com", "www.civitai.com"}:
        return DownloadProvider.CIVITAI
    if host == "huggingface.co":
        return DownloadProvider.HUGGING_FACE
    return None


def acquisition_failure(error: BaseException | None = None, *, url: str = "") -> AcquisitionFailure:
    """Classify typed causes; neither exception strings nor responses are displayed.

    Provider guidance uses the catalog source, not a redirect's possibly unrelated
    CDN host. A 403 is a refusal, not proof that a missing token caused it.
    """
    provider = _provider(url)
    cause = error
    seen: set[int] = set()
    retained = False
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        if isinstance(cause, DownloadError):
            retained = retained or cause.partials_retained
        if (
            isinstance(
                cause, (ChecksumMismatchError, httpx.HTTPStatusError, httpx.TransportError, OSError)
            )
            or cause.__cause__ is None
        ):
            break
        cause = cause.__cause__

    facts = {
        "provider": provider,
        "error_type": type(cause).__name__ if cause is not None else None,
        "partials_retained": retained,
    }
    if isinstance(cause, ChecksumMismatchError):
        return AcquisitionFailure(
            "model-download-checksum",
            "Checksum divergente: o arquivo inválido foi descartado. Tente baixar novamente.",
            **facts,
        )
    if isinstance(cause, httpx.HTTPStatusError):
        status = cause.response.status_code
        facts["http_status"] = status
        if status in {401, 403}:
            refusal = "Autenticação recusada" if status == 401 else "Acesso negado"
            if provider is DownloadProvider.CIVITAI:
                guidance = (
                    "Confira o token do Civitai em Configurações e o acesso/licença do modelo na sua conta. "
                    "Depois, tente novamente; o token não libera conteúdo restrito."
                )
            elif provider is DownloadProvider.HUGGING_FACE:
                guidance = (
                    "Confira o token de leitura do Hugging Face em Configurações e aceite a licença/solicite "
                    "acesso ao modelo na sua conta. Depois, tente novamente."
                )
            else:
                guidance = (
                    "Confira autenticação, permissão e licença na fonte do modelo. "
                    "Depois de liberar o acesso, tente novamente."
                )
            message = f"{refusal} (HTTP {status}). {guidance}"
        elif status == 429:
            message = (
                "Limite de downloads da fonte (HTTP 429). Aguarde e tente novamente manualmente."
            )
        else:
            message = (
                f"A fonte recusou o download (HTTP {status}). "
                "Confira a disponibilidade e o acesso ao modelo na fonte; depois, tente novamente."
            )
        return AcquisitionFailure(f"model-download-http-{status}", message, **facts)
    if isinstance(cause, httpx.TransportError):
        timeout = isinstance(cause, httpx.TimeoutException)
        reason = "Tempo de conexão esgotado" if timeout else "Conexão com a fonte interrompida"
        resume = (
            "Os bytes parciais foram mantidos para retomar quando a fonte permitir."
            if retained
            else "A próxima tentativa aproveita bytes parciais disponíveis quando a fonte permitir."
        )
        return AcquisitionFailure(
            "model-download-timeout" if timeout else "model-download-transport",
            f"{reason}. Verifique a conexão e tente novamente. {resume}",
            **facts,
        )
    if isinstance(cause, OSError):
        facts.update(os_errno=cause.errno, os_winerror=getattr(cause, "winerror", None))
        if cause.errno == errno.ENOSPC or facts["os_winerror"] in {39, 112}:
            return AcquisitionFailure(
                "model-download-disk-full",
                "Sem espaço para baixar o modelo. Libere espaço no disco da instalação e tente novamente.",
                **facts,
            )
        if (
            isinstance(cause, PermissionError)
            or cause.errno in {errno.EACCES, errno.EPERM}
            or facts["os_winerror"] == 5
        ):
            return AcquisitionFailure(
                "model-download-access-denied",
                "O sistema negou acesso ao arquivo do modelo. Confira a permissão de gravação e bloqueios "
                "de segurança na pasta da instalação; depois, tente novamente.",
                **facts,
            )
        return AcquisitionFailure(
            "model-download-filesystem",
            "Não foi possível gravar ou acessar o arquivo do modelo. Confira o disco e a pasta da "
            "instalação; depois, tente novamente.",
            **facts,
        )
    return AcquisitionFailure(
        "model-download-failed",
        "Não foi possível concluir a aquisição do modelo. Tente novamente; se persistir, "
        "confira a disponibilidade e o acesso ao modelo na fonte.",
        **facts,
    )
