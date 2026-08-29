"""Testes do detector.

Nenhum teste chama nvidia-smi ou git de verdade: o CommandRunner e substituido
por um FakeRunner que devolve outputs canonicos.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import fields

import pytest

from local_ai_stack.runner import CommandResult, CommandRunner
from local_ai_stack.system import (
    GPUInfo,
    NvidiaFacts,
    SystemFacts,
    detect_git,
    detect_nvidia,
    detect_system,
)

# --- fixtures de output ----------------------------------------------------

XML_ONE_GPU = """<?xml version="1.0" ?>
<nvidia_smi_log>
  <driver_version>566.36</driver_version>
  <cuda_version>12.7</cuda_version>
  <attached_gpus>1</attached_gpus>
  <gpu id="00000000:01:00.0">
    <product_name>NVIDIA GeForce RTX 5070</product_name>
    <uuid>GPU-1111</uuid>
    <minor_number>0</minor_number>
    <fb_memory_usage>
      <total>12288 MiB</total>
      <free>11500 MiB</free>
      <used>788 MiB</used>
    </fb_memory_usage>
  </gpu>
</nvidia_smi_log>
"""

XML_TWO_GPUS = """<?xml version="1.0" ?>
<nvidia_smi_log>
  <driver_version>566.36</driver_version>
  <cuda_version>12.7</cuda_version>
  <gpu id="00000000:01:00.0">
    <product_name>NVIDIA GeForce RTX 5090</product_name>
    <uuid>GPU-AAAA</uuid>
    <minor_number>0</minor_number>
    <fb_memory_usage>
      <total>32768 MiB</total>
      <free>32000 MiB</free>
    </fb_memory_usage>
  </gpu>
  <gpu id="00000000:02:00.0">
    <product_name>NVIDIA GeForce RTX 5070</product_name>
    <uuid>GPU-BBBB</uuid>
    <minor_number>1</minor_number>
    <fb_memory_usage>
      <total>12288 MiB</total>
      <free>[N/A]</free>
    </fb_memory_usage>
  </gpu>
</nvidia_smi_log>
"""

CSV_TWO_GPUS = "0, NVIDIA GeForce RTX 5090, 32768, 32000, 566.36\n1, NVIDIA GeForce RTX 5070, 12288, 11500, 566.36\n"


class FakeRunner(CommandRunner):
    """CommandRunner com PATH e outputs controlados. Nao executa nada."""

    def __init__(
        self,
        *,
        binaries: Mapping[str, str] | None = None,
        outputs: Mapping[str, CommandResult] | None = None,
    ) -> None:
        super().__init__()
        self.binaries = dict(binaries if binaries is not None else {"nvidia-smi": "C:\\smi.exe"})
        self.outputs = dict(outputs or {})
        self.calls: list[tuple[str, ...]] = []
        self.call_kwargs: list[dict[str, object]] = []

    def which(self, name: str, extra_env: Mapping[str, str] | None = None) -> str | None:
        return self.binaries.get(name)

    def run(self, argv: Sequence[str], **kwargs: object) -> CommandResult:
        items = tuple(str(a) for a in argv)
        self.calls.append(items)
        self.call_kwargs.append(dict(kwargs))
        result = self.outputs.get(self._key(items))
        if result is None:
            return CommandResult(argv=items, returncode=127, stderr="sem fixture", not_found=True)
        return result

    @staticmethod
    def _key(argv: Sequence[str]) -> str:
        if argv[0] == "git":
            return "git"
        if "-x" in argv:
            return "xml"
        return "csv"

    def count(self, key: str) -> int:
        return sum(1 for c in self.calls if self._key(c) == key)


def ok(stdout: str, argv: tuple[str, ...] = ("nvidia-smi",)) -> CommandResult:
    return CommandResult(argv=argv, returncode=0, stdout=stdout)


def fail(returncode: int = 9, stderr: str = "boom") -> CommandResult:
    return CommandResult(argv=("nvidia-smi",), returncode=returncode, stderr=stderr)


GIT_OK = {
    "git": CommandResult(
        argv=("git", "--version"), returncode=0, stdout="git version 2.45.1.windows.1\n"
    )
}


def runner_with(**outputs: CommandResult) -> FakeRunner:
    return FakeRunner(
        binaries={"nvidia-smi": "C:\\smi.exe", "git": "C:\\git.exe"},
        outputs={**GIT_OK, **outputs},
    )


# --- NVIDIA ausente --------------------------------------------------------


def test_nvidia_absent_is_not_an_error():
    r = FakeRunner(binaries={})
    n = detect_nvidia(r)
    assert n.smi_available is False
    assert n.query_ok is False
    assert n.gpus == ()
    assert n.gpu_count == 0
    assert n.driver_version is None
    assert n.cuda_version_supported_by_driver is None
    assert n.error is not None
    assert r.calls == []  # nao tenta executar o que nao existe


def test_system_without_nvidia_still_reports_facts():
    f = detect_system(FakeRunner(binaries={"git": "C:\\git.exe"}, outputs=GIT_OK))
    assert f.nvidia_smi_available is False
    assert f.gpus == ()
    assert f.os_name
    assert f.python_version


# --- nvidia-smi falha ------------------------------------------------------


def test_smi_failure_is_reported_not_raised():
    n = detect_nvidia(runner_with(xml=fail(9, "insufficient permissions"), csv=fail(9, "boom")))
    assert n.smi_available is True
    assert n.query_ok is False
    assert n.gpus == ()
    assert n.error is not None
    assert "9" in n.error


def test_smi_timeout_is_reported():
    timed_out = CommandResult(argv=("nvidia-smi",), returncode=-1, timed_out=True)
    n = detect_nvidia(runner_with(xml=timed_out, csv=timed_out))
    assert n.query_ok is False
    assert "timeout" in n.error


# --- uma GPU ---------------------------------------------------------------


def test_single_gpu():
    n = detect_nvidia(runner_with(xml=ok(XML_ONE_GPU)))
    assert n.query_ok is True
    assert n.gpu_count == 1
    g = n.gpus[0]
    assert g.name == "NVIDIA GeForce RTX 5070"
    assert g.vram_total_mib == 12288
    assert g.vram_free_mib == 11500
    assert g.index == 0
    assert g.uuid == "GPU-1111"


def test_single_call_in_happy_path():
    """Requisito: nao martelar o nvidia-smi."""
    r = runner_with(xml=ok(XML_ONE_GPU))
    detect_nvidia(r)
    assert r.count("xml") == 1
    assert r.count("csv") == 0


def test_detect_system_queries_smi_once():
    r = runner_with(xml=ok(XML_ONE_GPU))
    detect_system(r)
    assert r.count("xml") == 1
    assert r.count("csv") == 0


# --- multiplas GPUs --------------------------------------------------------


def test_multiple_gpus_keep_order_and_own_values():
    n = detect_nvidia(runner_with(xml=ok(XML_TWO_GPUS)))
    assert n.gpu_count == 2
    assert [g.name for g in n.gpus] == [
        "NVIDIA GeForce RTX 5090",
        "NVIDIA GeForce RTX 5070",
    ]
    assert [g.vram_total_mib for g in n.gpus] == [32768, 12288]
    assert [g.index for g in n.gpus] == [0, 1]
    assert n.gpus[0].vram_free_mib == 32000
    assert n.gpus[1].vram_free_mib is None  # [N/A] nao vira zero


def test_multiple_gpus_via_csv():
    n = detect_nvidia(runner_with(xml=fail(), csv=ok(CSV_TWO_GPUS)))
    assert n.query_ok is True
    assert n.source == "csv"
    assert [g.vram_total_mib for g in n.gpus] == [32768, 12288]
    assert n.driver_version == "566.36"
    assert n.cuda_version_supported_by_driver is None  # csv nao traz esse dado


# --- parsing de VRAM -------------------------------------------------------


def test_vram_is_integer_mib_never_float_gb():
    g = detect_nvidia(runner_with(xml=ok(XML_ONE_GPU))).gpus[0]
    assert isinstance(g.vram_total_mib, int)
    assert isinstance(g.vram_free_mib, int)
    assert g.vram_total_bytes == 12288 * 1024 * 1024
    assert g.vram_free_bytes == 11500 * 1024 * 1024
    assert g.vram_used_mib == 788


def test_vram_free_absent_keeps_derived_values_none():
    g = GPUInfo(name="x", vram_total_mib=8192)
    assert g.vram_free_mib is None
    assert g.vram_free_bytes is None
    assert g.vram_used_mib is None


@pytest.mark.parametrize(
    "total, expected",
    [
        ("12288 MiB", 12288),
        ("12288", 12288),
        ("  12288   MiB  ", 12288),
        ("[N/A]", None),
        ("", None),
        ("lixo", None),
    ],
)
def test_vram_parsing_variants(total, expected):
    xml = XML_ONE_GPU.replace("<total>12288 MiB</total>", f"<total>{total}</total>")
    n = detect_nvidia(runner_with(xml=ok(xml), csv=fail()))
    if expected is None:
        assert n.gpus == ()  # GPU sem VRAM legivel e descartada, nao vira zero
    else:
        assert n.gpus[0].vram_total_mib == expected


# --- driver / CUDA ---------------------------------------------------------


def test_driver_and_cuda_parsing():
    n = detect_nvidia(runner_with(xml=ok(XML_ONE_GPU)))
    assert n.driver_version == "566.36"
    assert n.cuda_version_supported_by_driver == "12.7"


def test_cuda_field_is_driver_capability_not_toolkit():
    """O campo existe para nao ser confundido com CUDA Toolkit instalado."""
    names = {f.name for f in fields(NvidiaFacts)}
    assert "cuda_version_supported_by_driver" in names
    assert "cuda_version" not in names
    assert "toolkit" not in " ".join(names)
    assert "NAO significa que o CUDA" in NvidiaFacts.__doc__


def test_missing_driver_and_cuda_are_none_not_empty_string():
    xml = XML_ONE_GPU.replace("<driver_version>566.36</driver_version>", "").replace(
        "<cuda_version>12.7</cuda_version>", "<cuda_version>[N/A]</cuda_version>"
    )
    n = detect_nvidia(runner_with(xml=ok(xml)))
    assert n.driver_version is None
    assert n.cuda_version_supported_by_driver is None
    assert n.gpus  # GPU continua detectada


# --- output malformado -----------------------------------------------------


def test_malformed_xml_falls_back_to_csv():
    r = runner_with(xml=ok("<nvidia_smi_log><gpu>truncad"), csv=ok(CSV_TWO_GPUS))
    n = detect_nvidia(r)
    assert n.query_ok is True
    assert n.source == "csv"
    assert n.gpu_count == 2
    assert r.count("xml") == 1 and r.count("csv") == 1


def test_malformed_everywhere_reports_error_without_raising():
    n = detect_nvidia(runner_with(xml=ok("nao e xml"), csv=ok("lixo,,,")))
    assert n.smi_available is True
    assert n.query_ok is False
    assert n.gpus == ()
    assert n.error is not None


def test_empty_output_is_not_a_crash():
    n = detect_nvidia(runner_with(xml=ok(""), csv=ok("")))
    assert n.query_ok is False
    assert n.gpus == ()


def test_csv_tolerates_comma_in_gpu_name():
    n = detect_nvidia(
        runner_with(xml=fail(), csv=ok("0, NVIDIA RTX A6000, Ada, 49140, 48000, 566.36\n"))
    )
    assert n.gpus[0].name == "NVIDIA RTX A6000, Ada"
    assert n.gpus[0].vram_total_mib == 49140


def test_csv_skips_broken_lines_but_keeps_good_ones():
    csv = "linha quebrada\n1, NVIDIA GeForce RTX 5070, 12288, 11500, 566.36\n\n"
    n = detect_nvidia(runner_with(xml=fail(), csv=ok(csv)))
    assert n.gpu_count == 1
    assert n.gpus[0].index == 1


# --- Git -------------------------------------------------------------------


def test_git_absent():
    r = FakeRunner(binaries={"nvidia-smi": "C:\\smi.exe"}, outputs={"xml": ok(XML_ONE_GPU)})
    path, version = detect_git(r)
    assert path is None and version is None
    f = detect_system(r)
    assert f.git_available is False
    assert f.git_version is None and f.git_path is None


def test_git_version_parsed():
    r = runner_with(xml=ok(XML_ONE_GPU))
    path, version = detect_git(r)
    assert path == "C:\\git.exe"
    assert version == "2.45.1.windows.1"


def test_git_present_but_failing_keeps_path_without_version():
    r = FakeRunner(
        binaries={"git": "C:\\git.exe"},
        outputs={"git": CommandResult(argv=("git",), returncode=1, stderr="broken")},
    )
    path, version = detect_git(r)
    assert path == "C:\\git.exe"
    assert version is None
    assert detect_system(r).git_available is True


def test_git_unexpected_output_does_not_crash():
    r = FakeRunner(
        binaries={"git": "C:\\git.exe"},
        outputs={"git": CommandResult(argv=("git",), returncode=0, stdout="something else\n")},
    )
    assert detect_git(r) == ("C:\\git.exe", None)


# --- preservacao da API ----------------------------------------------------


def test_public_names_still_exist():
    from local_ai_stack import system

    for name in ("GPUInfo", "SystemFacts", "detect_system"):
        assert hasattr(system, name)


def test_detect_system_works_with_no_arguments():
    f = detect_system()
    assert isinstance(f, SystemFacts)
    assert isinstance(f.gpus, tuple)
    assert isinstance(f.git_available, bool)
    assert isinstance(f.nvidia_smi_available, bool)


def test_legacy_field_order_is_preserved():
    names = [f.name for f in fields(SystemFacts)]
    assert names[:6] == [
        "os_name",
        "os_release",
        "python_version",
        "git_available",
        "nvidia_smi_available",
        "gpus",
    ]


def test_legacy_gpuinfo_positional_construction_and_vram_mb():
    g = GPUInfo("NVIDIA GeForce RTX 5070", 12288)
    assert g.vram_mb == 12288 == g.vram_total_mib
    assert f"{g.vram_mb / 1024:.1f}" == "12.0"  # como TUI e doctor renderizam


def test_detected_gpus_expose_legacy_vram_mb():
    f = detect_system(runner_with(xml=ok(XML_ONE_GPU)))
    assert f.gpus[0].vram_mb == 12288
    assert f.gpus is f.nvidia.gpus


def test_new_system_facts_are_populated():
    f = detect_system(runner_with(xml=ok(XML_ONE_GPU)))
    assert f.machine
    assert f.architecture
    assert f.os_version
    assert f.python_executable
    assert f.is_windows == (f.os_name.lower() == "windows")
    assert f.nvidia.query_ok is True


@pytest.mark.parametrize(
    "version, expected",
    [("10.0.26200", 26200), ("10.0.19045", 19045), ("10.0", None), ("", None), ("a.b.c", None)],
)
def test_windows_build_parsing(version, expected, monkeypatch):
    monkeypatch.setattr("local_ai_stack.system.platform.version", lambda: version)
    f = detect_system(runner_with(xml=ok(XML_ONE_GPU)))
    assert f.windows_build == expected
    assert f.os_version == version


def test_facts_are_immutable():
    f = detect_system(runner_with(xml=ok(XML_ONE_GPU)))
    with pytest.raises(AttributeError):
        f.os_name = "Linux"
    with pytest.raises(AttributeError):
        f.gpus[0].vram_total_mib = 1


def test_detection_forces_dry_run_false_because_it_is_read_only():
    """Um runner em dry-run nao pode cegar o detector: deteccao nao muda nada."""
    r = runner_with(xml=ok(XML_ONE_GPU))
    r.dry_run = True
    detect_system(r)
    assert r.call_kwargs
    assert all(kw.get("dry_run") is False for kw in r.call_kwargs)


def test_real_runner_in_dry_run_still_detects(monkeypatch):
    real = CommandRunner(dry_run=True)
    monkeypatch.setattr(real, "which", lambda name, extra_env=None: None)
    f = detect_system(real)
    assert f.nvidia_smi_available is False
    assert f.git_available is False
