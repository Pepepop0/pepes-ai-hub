"""Backend authority over the frozen external-conflict contract.

This module turns one raw :class:`~local_ai_stack.integration.ManagedPreflightReport`
into the typed, safe-to-show :class:`~local_ai_stack.external_conflict_contracts.ConflictPreflightReport`,
and is the only place that may act on a chosen
:class:`~local_ai_stack.external_conflict_contracts.ExternalConflictPolicy`.

Nothing here decides *whether* a conflict exists -- ``ManagedPreflightCode.ALONGSIDE_REQUIRED``
already says that, and stays exactly as strict as it always was, for every
component the existing coexistence tests already lock down. What was missing
is a way to *resolve* it instead of failing outright: this module reads the
same detection and listener-scan primitives the preflight already used, adds
one thing they cannot carry across a process boundary -- proof of a running
process's identity -- and answers the two remaining questions: can this be
installed beside the external service on an isolated endpoint, and can this
specific, already-observed external process be told to stop.

Stopping is intentionally narrower than anything else in this codebase.
:mod:`local_ai_stack.launcher` proves ownership through a kernel job this
process created, which by definition cannot apply to something nobody here
ever started. The only proof available for an external process is
``(pid, executable path, process start time)``, captured at the moment the
conflict was observed and re-checked immediately before acting -- never a
name search, never a bare caller-supplied pid, never a second escalation.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from uuid import uuid4

try:
    import psutil
except ImportError:  # pragma: no cover - psutil is a declared dependency
    psutil = None

from .components import ComponentId
from .external_conflict_contracts import (
    ConflictEndpoint,
    ConflictHardBlocker,
    ConflictPolicySelection,
    ConflictPreflightReport,
    ConflictResolutionResult,
    ConflictResolutionStatus,
    ExternalConflictKind,
    ExternalConflictOrigin,
    ExternalConflictPolicy,
    ExternalServiceConflict,
)
from .integration import IntegrationOptions, ManagedPreflightCode, ManagedPreflightReport
from .inventory import ListenerReport, LookupOutcome, ScanOutcome
from .inventory import list_listeners as default_list_listeners
from .inventory import lookup_process as default_lookup_process
from .operations import safe_text
from .profiles import Profile

# Deterministic, fixed alternate endpoints offered to INSTALL_ALONGSIDE. Chosen
# once and never derived from whatever happens to be free right now, matching
# the isolated ports this codebase already uses wherever a managed stack needs
# to coexist with an external one (see the e2e controlled world and the wave-A
# coexistence tests). Never allocated on demand; only ever offered, and only
# after confirming the port is actually free.
ALONGSIDE_PORTS: Mapping[str, int] = {
    str(ComponentId.OLLAMA): 21434,
    str(ComponentId.OPEN_WEBUI): 28080,
    str(ComponentId.COMFYUI): 28188,
}

_DISPLAY = {
    str(ComponentId.OLLAMA): "Ollama",
    str(ComponentId.OPEN_WEBUI): "Open WebUI",
    str(ComponentId.COMFYUI): "ComfyUI",
}

# How long a STOP is given to actually free the port before the resolution
# reports failure rather than trusting the stop call's word for it.
STOP_TIMEOUT_SECONDS = 10.0
STOP_POLL_SECONDS = 0.5


class ConflictResolutionRequired(Exception):
    """A resolvable external conflict exists; the caller must supply a policy.

    Raised before any operation is created and before any mutation happens --
    there is nothing to fail yet. ``report`` carries everything a chooser needs:
    :attr:`~ConflictPreflightReport.available_policies` and
    :attr:`~ConflictPreflightReport.recommended_policy`.
    """

    def __init__(self, report: ConflictPreflightReport) -> None:
        super().__init__("external conflicts require a policy before this can continue")
        self.report = report


@dataclass(frozen=True)
class _ConflictEvidence:
    """Private: what proved one conflict. Never leaves this module."""

    service_id: str
    host: str
    port: int
    pid: int
    exe: str | None
    create_time: float

    def matches(self, exe: str | None, create_time: float | None) -> bool:
        return create_time is not None and create_time == self.create_time and exe == self.exe


def merged_options(
    base: IntegrationOptions, report: ConflictPreflightReport, policy: ExternalConflictPolicy
) -> IntegrationOptions:
    """The durable options a resolved report implies.

    Every conflicting component gets ``alongside_external`` from here on: even
    for STOP, the external's files stay on disk (nothing here uninstalls
    anything), so the provisioner keeps calling it EXTERNAL and would otherwise
    skip installing our own copy. INSTALL_ALONGSIDE additionally claims the
    isolated endpoint the report already verified was free; STOP does not,
    because the default endpoint is exactly what stopping was for.
    """
    components = {str(conflict.service_id) for conflict in report.conflicts}
    overrides = dict(base.port_overrides)
    if policy is ExternalConflictPolicy.INSTALL_ALONGSIDE:
        for conflict in report.conflicts:
            if conflict.managed_endpoint is not None:
                overrides[str(conflict.service_id)] = conflict.managed_endpoint.port
    return IntegrationOptions(
        host=base.host,
        port_overrides=overrides,
        alongside_external=frozenset(base.alongside_external | components),
    )


def _occupants(listeners: ListenerReport, host: str, port: int, managed_pids: frozenset[int]):
    return [
        listener
        for listener in listeners.on_port(port, host)
        if listener.pid is None or listener.pid not in managed_pids
    ]


def _hard_blocker(issue) -> ConflictHardBlocker:
    return ConflictHardBlocker(str(issue.code), safe_text(issue.detail))


def terminate_by_pid(pid: int, create_time: float) -> bool:
    """Ask one already identity-confirmed process to end. No tree, no retry.

    Deliberately not :mod:`local_ai_stack.launcher`: that module proves
    ownership through a kernel job this process created, which cannot apply
    here by definition. The caller has already matched ``(pid, exe,
    create_time)`` immediately before this call; ``create_time`` is re-checked
    once more here, right at the OS call, to close the gap between that check
    and this one. A process that ignores the request is reported as still
    running by the caller's own port re-check, never force-escalated.

    This is the seam a test replaces: bound through
    :attr:`~local_ai_stack.stack_application.StackDependencies.terminate_process`,
    so a controlled world can prove a stop happened without this process ever
    touching a real one.
    """
    if psutil is None:  # pragma: no cover - psutil is a declared dependency
        return False
    try:
        process = psutil.Process(pid)
        if process.create_time() != create_time:
            return False  # the pid was recycled between proof and this call
        process.terminate()
        return True
    except psutil.NoSuchProcess:
        return True  # already gone is the outcome we wanted
    except psutil.Error:
        return False


class ExternalConflictSession:
    """One application's private evidence store, across one attempt at a time.

    Implements :class:`~local_ai_stack.external_conflict_contracts.ExternalConflictResolver`.
    A fresh :meth:`observe` replaces whatever the previous attempt held; a
    preflight id is usable exactly once by :meth:`resolve_external_conflicts`.
    """

    def __init__(
        self,
        *,
        lookup_process: Callable[[int], object] = default_lookup_process,
        list_listeners: Callable[[], ListenerReport] = default_list_listeners,
        alongside_ports: Mapping[str, int] = ALONGSIDE_PORTS,
        terminate_process: Callable[[int, float], bool] = terminate_by_pid,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._lookup_process = lookup_process
        self._list_listeners = list_listeners
        self._alongside_ports = dict(alongside_ports)
        self._terminate = terminate_process
        self._monotonic = monotonic
        self._sleep = sleep
        self._report: ConflictPreflightReport | None = None
        self._evidence: dict[str, _ConflictEvidence] = {}
        self._managed_pids: frozenset[int] = frozenset()
        self._consumed: set[str] = set()

    # --- construção do relatório tipado -------------------------------------

    def observe(
        self,
        *,
        profile: Profile,
        options: IntegrationOptions,
        raw_report: ManagedPreflightReport,
        listeners: ListenerReport,
        managed_pids: frozenset[int],
    ) -> ConflictPreflightReport | None:
        """A typed, resolvable view of the alongside-required issues, or None.

        Every other issue in ``raw_report`` (incompatible hardware, low disk, an
        unusable listener scan, an unrelated duplicate endpoint, a port squatted
        by something that is not a known external install) is left exactly
        where it already lived: as a hard failure the normal preflight step
        reports on its own. This is only about the issues a policy can resolve.
        """
        alongside_ids = sorted(
            {
                issue.component_id
                for issue in raw_report.issues
                if issue.code is ManagedPreflightCode.ALONGSIDE_REQUIRED
                and issue.component_id is not None
            }
        )
        if not alongside_ids:
            self._report = None
            self._evidence = {}
            return None

        endpoints = options.endpoints_for(profile)
        hard_blockers = [
            _hard_blocker(issue)
            for issue in raw_report.issues
            if not (
                issue.component_id in alongside_ids
                and issue.code
                in (
                    ManagedPreflightCode.ALONGSIDE_REQUIRED,
                    ManagedPreflightCode.ENDPOINT_COLLISION,
                )
            )
        ]

        conflicts: list[ExternalServiceConflict] = []
        evidence: dict[str, _ConflictEvidence] = {}
        for component_id in alongside_ids:
            spec = endpoints.spec(component_id)
            endpoint = ConflictEndpoint(spec.host, spec.port)
            occupants = _occupants(listeners, spec.host, spec.port, managed_pids)
            pids = sorted({item.pid for item in occupants if item.pid is not None})

            proof: _ConflictEvidence | None = None
            if len(pids) == 1:
                proof = self._prove(component_id, spec.host, spec.port, pids[0])
                if proof is not None:
                    evidence[component_id] = proof

            managed_endpoint = self._alongside_endpoint(
                component_id, spec.host, listeners, managed_pids
            )
            conflicts.append(
                ExternalServiceConflict(
                    service_id=ComponentId(component_id),
                    kind=ExternalConflictKind.ENDPOINT_OCCUPIED,
                    origin=ExternalConflictOrigin.EXTERNAL,
                    endpoint=endpoint,
                    reason=safe_text(
                        f"{_DISPLAY.get(component_id, component_id)} externo está em uso "
                        f"em {endpoint.display}."
                    ),
                    can_stop_safely=proof is not None,
                    managed_endpoint=managed_endpoint,
                )
            )
            if managed_endpoint is None and proof is None:
                # Neither policy is safe: say so explicitly rather than present
                # a choice with nothing behind it.
                hard_blockers.append(
                    ConflictHardBlocker(
                        "managed-endpoint-occupied",
                        safe_text(
                            f"{_DISPLAY.get(component_id, component_id)}: nenhuma resolução "
                            "segura está disponível para este conflito."
                        ),
                    )
                )

        report = ConflictPreflightReport(uuid4().hex, tuple(conflicts), tuple(hard_blockers))
        self._report = report
        self._evidence = evidence
        self._managed_pids = managed_pids
        return report

    def _prove(self, component_id: str, host: str, port: int, pid: int) -> _ConflictEvidence | None:
        lookup = self._lookup_process(pid)
        if lookup.outcome is not LookupOutcome.FOUND or lookup.process is None:
            return None
        if lookup.process.create_time is None:
            return None
        return _ConflictEvidence(
            component_id, host, port, pid, lookup.process.exe, lookup.process.create_time
        )

    def _alongside_endpoint(
        self,
        component_id: str,
        host: str,
        listeners: ListenerReport,
        managed_pids: frozenset[int],
    ) -> ConflictEndpoint | None:
        port = self._alongside_ports.get(component_id)
        if port is None:
            return None
        if _occupants(listeners, host, port, managed_pids):
            return None
        return ConflictEndpoint(host, port)

    # --- protocolo -----------------------------------------------------------

    def current_conflict_preflight(self) -> ConflictPreflightReport | None:
        """The pending conflict, or ``None`` once it has been resolved.

        A consumed report is not forgotten -- :meth:`resolve_external_conflicts`
        still refuses to reuse its id -- it simply stops being "current": there
        is nothing left waiting for a chooser.
        """
        if self._report is None or self._report.preflight_id in self._consumed:
            return None
        return self._report

    def resolve_external_conflicts(
        self, selection: ConflictPolicySelection
    ) -> ConflictResolutionResult:
        report = self._report
        if report is None or report.preflight_id != selection.preflight_id:
            raise ValueError("selection refers to a stale or unknown preflight")
        if selection.preflight_id in self._consumed:
            raise ValueError("this preflight has already been resolved")
        report.validate_selection(selection)
        self._consumed.add(selection.preflight_id)

        if selection.policy is ExternalConflictPolicy.ABORT:
            return ConflictResolutionResult(
                selection.preflight_id,
                selection.policy,
                ConflictResolutionStatus.ABORTED,
                "Instalação cancelada.",
            )
        if selection.policy is ExternalConflictPolicy.INSTALL_ALONGSIDE:
            return ConflictResolutionResult(
                selection.preflight_id,
                selection.policy,
                ConflictResolutionStatus.PROCEED,
                "Instalando ao lado das instalações existentes.",
            )
        return self._stop_conflicts(report, selection)

    def _stop_conflicts(
        self, report: ConflictPreflightReport, selection: ConflictPolicySelection
    ) -> ConflictResolutionResult:
        """Narrow authority: only the conflicts *this* preflight produced.

        Every step below can refuse; none of them can guess. A gap in evidence,
        a mismatch on re-check, or a port that never clears, and this reports
        FAILED without touching anything further.
        """
        for conflict in report.conflicts:
            service_id = str(conflict.service_id)
            name = _DISPLAY.get(service_id, service_id)
            proof = self._evidence.get(service_id)
            if proof is None or not conflict.can_stop_safely:
                return self._stop_failed(
                    selection,
                    f"Não há identidade suficiente para parar {name} com segurança; "
                    "nada foi encerrado.",
                )
            fresh = self._lookup_process(proof.pid)
            if fresh.outcome is not LookupOutcome.FOUND or fresh.process is None:
                return self._stop_failed(
                    selection, f"{name} não pôde ser confirmado; nada foi encerrado."
                )
            if not proof.matches(fresh.process.exe, fresh.process.create_time):
                return self._stop_failed(
                    selection,
                    f"O processo em {name} mudou desde a verificação; nada foi encerrado.",
                )
            if not self._terminate(proof.pid, proof.create_time):
                return self._stop_failed(selection, f"Não foi possível parar {name}.")
            if not self._wait_for_free(proof):
                return self._stop_failed(
                    selection, f"{name} não liberou {proof.host}:{proof.port} a tempo."
                )
        return ConflictResolutionResult(
            selection.preflight_id,
            selection.policy,
            ConflictResolutionStatus.PROCEED,
            "Serviços externos conflitantes foram parados.",
        )

    def _stop_failed(
        self, selection: ConflictPolicySelection, message: str
    ) -> ConflictResolutionResult:
        return ConflictResolutionResult(
            selection.preflight_id, selection.policy, ConflictResolutionStatus.FAILED, message
        )

    def _wait_for_free(self, proof: _ConflictEvidence) -> bool:
        deadline = self._monotonic() + STOP_TIMEOUT_SECONDS
        while True:
            listeners = self._list_listeners()
            if listeners.outcome is ScanOutcome.OK and not _occupants(
                listeners, proof.host, proof.port, self._managed_pids
            ):
                return True
            if self._monotonic() >= deadline:
                return False
            self._sleep(STOP_POLL_SECONDS)


__all__ = [
    "ALONGSIDE_PORTS",
    "STOP_POLL_SECONDS",
    "STOP_TIMEOUT_SECONDS",
    "ConflictResolutionRequired",
    "ExternalConflictSession",
    "merged_options",
    "terminate_by_pid",
]
