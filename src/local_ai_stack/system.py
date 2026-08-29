"""Detector de fatos do ambiente.

Este modulo SO detecta e reporta. Ele nao decide nada:
- nao conhece profiles (Pepe/Capella/Test);
- nao aplica regra de compatibilidade nem tolerancia de VRAM;
- nao decide o que instalar.

Quem decide e o compatibility engine, a partir destes fatos.

Toda chamada externa passa pelo CommandRunner, que e injetavel para testes.
Deteccao e read-only por definicao, entao os comandos aqui sao executados com
`dry_run=False` mesmo quando o runner recebido esta em modo dry-run.

VRAM: a fonte de verdade e inteiro em MiB (`vram_total_mib`, `vram_free_mib`),
nunca float de GB. Conversoes para GB sao apenas apresentacao.
"""

from __future__ import annotations

import platform
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from .runner import CommandResult, CommandRunner

NVIDIA_SMI = "nvidia-smi"
GIT = "git"

DEFAULT_DETECT_TIMEOUT = 10.0
MIB = 1024 * 1024

# Uma unica chamada traz driver, CUDA suportada e memoria de todas as GPUs.
_SMI_XML_ARGS = ("-q", "-x")
# Fallback estruturado caso o XML falhe ou venha ilegivel. Nao traz CUDA version.
_SMI_CSV_ARGS = (
    "--query-gpu=index,name,memory.total,memory.free,driver_version",
    "--format=csv,noheader,nounits",
)


@dataclass(frozen=True)
class GPUInfo:
    """Fatos de uma GPU NVIDIA. Memoria sempre em MiB inteiro."""

    name: str
    vram_total_mib: int
    vram_free_mib: int | None = None
    index: int | None = None
    uuid: str | None = None

    @property
    def vram_mb(self) -> int:
        """Compat: nome antigo de `vram_total_mib`. Sempre foi MiB."""
        return self.vram_total_mib

    @property
    def vram_total_bytes(self) -> int:
        return self.vram_total_mib * MIB

    @property
    def vram_free_bytes(self) -> int | None:
        return None if self.vram_free_mib is None else self.vram_free_mib * MIB

    @property
    def vram_used_mib(self) -> int | None:
        if self.vram_free_mib is None:
            return None
        return max(0, self.vram_total_mib - self.vram_free_mib)


@dataclass(frozen=True)
class NvidiaFacts:
    """Fatos reportados pelo nvidia-smi.

    `cuda_version_supported_by_driver` e o campo "CUDA Version" do nvidia-smi:
    a maior versao de CUDA que ESTE DRIVER suporta. NAO significa que o CUDA
    Toolkit esteja instalado, nem que exista nvcc na maquina. Presenca do
    toolkit e um fato separado e nao e detectada aqui.
    """

    smi_available: bool = False
    smi_path: str | None = None
    query_ok: bool = False
    driver_version: str | None = None
    cuda_version_supported_by_driver: str | None = None
    gpus: tuple[GPUInfo, ...] = ()
    source: str | None = None
    error: str | None = None

    @property
    def gpu_count(self) -> int:
        return len(self.gpus)


@dataclass(frozen=True)
class SystemFacts:
    """Fatos do ambiente. Os seis primeiros campos preservam a ordem original."""

    os_name: str
    os_release: str
    python_version: str
    git_available: bool
    nvidia_smi_available: bool
    gpus: tuple[GPUInfo, ...]
    os_version: str = ""
    architecture: str = ""
    machine: str = ""
    python_executable: str = ""
    windows_build: int | None = None
    git_version: str | None = None
    git_path: str | None = None
    nvidia: NvidiaFacts = field(default_factory=NvidiaFacts)

    @property
    def is_windows(self) -> bool:
        return self.os_name.lower() == "windows"


def _parse_mib(raw: str | None) -> int | None:
    """Aceita '12288', '12288 MiB', ' 12288  MiB '. Rejeita '[N/A]' e lixo."""
    if raw is None:
        return None
    text = raw.strip()
    if not text or text.upper().startswith("[N"):
        return None
    head = text.split()[0].replace(",", "")
    try:
        value = int(float(head))
    except ValueError:
        return None
    return value if value >= 0 else None


def _clean(raw: str | None) -> str | None:
    if raw is None:
        return None
    text = raw.strip()
    if not text or text.upper().startswith("[N"):
        return None
    return text


def _windows_build(version: str) -> int | None:
    """Build de '10.0.26200'.

    Fato relevante porque `platform.release()` reporta "10" mesmo no Windows 11;
    so o build separa as duas versoes. Aqui isso e apenas reportado: qualquer
    regra sobre versao minima pertence ao compatibility engine.
    """
    parts = version.split(".")
    if len(parts) < 3:
        return None
    try:
        return int(parts[2])
    except ValueError:
        return None


def _runner(runner: CommandRunner | None) -> CommandRunner:
    return runner if runner is not None else CommandRunner(default_timeout=DEFAULT_DETECT_TIMEOUT)


def _parse_smi_xml(stdout: str) -> tuple[str | None, str | None, tuple[GPUInfo, ...]]:
    root = ET.fromstring(stdout)
    driver = _clean(root.findtext("driver_version"))
    cuda = _clean(root.findtext("cuda_version"))
    gpus: list[GPUInfo] = []
    for position, node in enumerate(root.findall("gpu")):
        name = _clean(node.findtext("product_name"))
        total = _parse_mib(node.findtext("fb_memory_usage/total"))
        if name is None or total is None:
            continue
        minor = _parse_mib(node.findtext("minor_number"))
        gpus.append(
            GPUInfo(
                name=name,
                vram_total_mib=total,
                vram_free_mib=_parse_mib(node.findtext("fb_memory_usage/free")),
                index=minor if minor is not None else position,
                uuid=_clean(node.findtext("uuid")),
            )
        )
    return driver, cuda, tuple(gpus)


def _parse_smi_csv(stdout: str) -> tuple[str | None, tuple[GPUInfo, ...]]:
    """Formato: index, name, memory.total, memory.free, driver_version."""
    driver: str | None = None
    gpus: list[GPUInfo] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            continue
        # nome pode conter virgula: tudo entre o index e os tres ultimos campos
        name = ", ".join(parts[1:-3]).strip()
        total = _parse_mib(parts[-3])
        if not name or total is None:
            continue
        driver = driver or _clean(parts[-1])
        gpus.append(
            GPUInfo(
                name=name,
                vram_total_mib=total,
                vram_free_mib=_parse_mib(parts[-2]),
                index=_parse_mib(parts[0]),
            )
        )
    return driver, tuple(gpus)


def detect_nvidia(runner: CommandRunner | None = None) -> NvidiaFacts:
    """Detecta driver, CUDA suportada pelo driver e memoria por GPU.

    Caminho feliz: uma unica chamada `nvidia-smi -q -x`. O fallback CSV so roda
    se o XML falhar ou nao render nenhuma GPU utilizavel.
    """
    run = _runner(runner)
    smi_path = run.which(NVIDIA_SMI)
    if smi_path is None:
        return NvidiaFacts(smi_available=False, error="nvidia-smi nao encontrado no PATH")

    xml_result = run.run(
        [NVIDIA_SMI, *_SMI_XML_ARGS], timeout=DEFAULT_DETECT_TIMEOUT, dry_run=False
    )
    if xml_result.ok:
        try:
            driver, cuda, gpus = _parse_smi_xml(xml_result.stdout)
        except ET.ParseError as exc:
            driver, cuda, gpus, xml_error = None, None, (), f"XML ilegivel: {exc}"
        else:
            xml_error = None if gpus else "XML sem GPU utilizavel"
        if gpus:
            return NvidiaFacts(
                smi_available=True,
                smi_path=smi_path,
                query_ok=True,
                driver_version=driver,
                cuda_version_supported_by_driver=cuda,
                gpus=gpus,
                source="xml",
            )
    else:
        xml_error = _smi_error(xml_result)

    csv_result = run.run(
        [NVIDIA_SMI, *_SMI_CSV_ARGS], timeout=DEFAULT_DETECT_TIMEOUT, dry_run=False
    )
    if not csv_result.ok:
        return NvidiaFacts(
            smi_available=True,
            smi_path=smi_path,
            query_ok=False,
            error=f"{xml_error}; csv: {_smi_error(csv_result)}",
        )
    csv_driver, csv_gpus = _parse_smi_csv(csv_result.stdout)
    if not csv_gpus:
        return NvidiaFacts(
            smi_available=True,
            smi_path=smi_path,
            query_ok=False,
            driver_version=csv_driver,
            error=f"{xml_error}; csv: nenhuma GPU utilizavel no output",
        )
    return NvidiaFacts(
        smi_available=True,
        smi_path=smi_path,
        query_ok=True,
        driver_version=csv_driver,
        cuda_version_supported_by_driver=None,
        gpus=csv_gpus,
        source="csv",
    )


def _smi_error(result: CommandResult) -> str:
    if result.timed_out:
        return "nvidia-smi excedeu o timeout"
    if result.not_found:
        return "nvidia-smi sumiu do PATH entre a resolucao e a execucao"
    lines = result.stderr.strip().splitlines()
    return f"nvidia-smi saiu com {result.returncode}: {lines[0] if lines else 'sem stderr'}"


def detect_git(runner: CommandRunner | None = None) -> tuple[str | None, str | None]:
    """Devolve (caminho do git, versao). Ambos None se o git nao estiver no PATH."""
    run = _runner(runner)
    git_path = run.which(GIT)
    if git_path is None:
        return None, None
    result = run.run([GIT, "--version"], timeout=DEFAULT_DETECT_TIMEOUT, dry_run=False)
    if not result.ok:
        return git_path, None
    text = result.stdout.strip()
    version = text.split()[2] if text.lower().startswith("git version") else None
    return git_path, version


def detect_system(runner: CommandRunner | None = None) -> SystemFacts:
    """Coleta todos os fatos do ambiente. `runner` e injetavel para testes."""
    run = _runner(runner)
    nvidia = detect_nvidia(run)
    git_path, git_version = detect_git(run)
    os_version = platform.version()
    return SystemFacts(
        os_name=platform.system(),
        os_release=platform.release(),
        python_version=platform.python_version(),
        git_available=git_path is not None,
        nvidia_smi_available=nvidia.smi_available,
        gpus=nvidia.gpus,
        os_version=os_version,
        architecture=platform.architecture()[0],
        machine=platform.machine(),
        python_executable=sys.executable,
        windows_build=_windows_build(os_version),
        git_version=git_version,
        git_path=git_path,
        nvidia=nvidia,
    )
