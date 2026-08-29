"""Layout de filesystem da stack instalada.

Fonte unica de verdade para onde as coisas moram em disco. Este modulo:
- resolve paths;
- valida conflitos;
- cria diretorios quando explicitamente mandado.

E nada alem disso. Nao baixa, nao instala, nao mexe em PATH nem registry,
nao cria atalho, nao conhece profiles.

Fronteira importante: aqui so existe o diretorio de INSTALACAO/RUNTIME.
O diretorio do repositorio Git e outra coisa e nao entra neste modulo. O clone
pode estar em qualquer lugar; a stack instalada nao depende dele nem do cwd.
Por isso `from_root` exige path absoluto: resolver relativo contra o cwd
reintroduziria exatamente a dependencia que queremos eliminar.

Construir um StackLayout nunca toca o disco. Criar diretorio e uma operacao
separada e explicita: `ensure()`.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_data_dir

APP_NAME = "LocalAIStack"
ENV_ROOT = "LOCAL_AI_STACK_ROOT"


class LayoutError(RuntimeError):
    """Path invalido ou em conflito com algo que ja existe em disco."""


def _collapse_duplicate_tail(path: Path) -> Path:
    """Evita LocalAIStack\\LocalAIStack quando a base ja termina no app name."""
    if path.name == APP_NAME and path.parent.name == APP_NAME:
        return path.parent
    return path


def default_root(env: Mapping[str, str] | None = None) -> Path:
    """Root padrao da instalacao.

    Ordem: variavel de ambiente explicita, senao o diretorio de dados do
    usuario. No Windows isso equivale a %LOCALAPPDATA%\\LocalAIStack.
    """
    environ = os.environ if env is None else env
    override = (environ.get(ENV_ROOT) or "").strip()
    if override:
        return _normalize_root(Path(override))
    base = Path(user_data_dir(appname=APP_NAME, appauthor=False, roaming=False))
    return _collapse_duplicate_tail(_normalize_root(base))


def _normalize_root(root: Path) -> Path:
    expanded = Path(root).expanduser()
    if not expanded.is_absolute():
        raise LayoutError(
            f"root da stack precisa ser um path absoluto, recebido {root!r}. "
            "Path relativo dependeria do cwd, e a stack instalada nao pode depender dele."
        )
    return Path(os.path.normpath(expanded))


def _safe_name(name: str) -> str:
    """Valida nome de arquivo simples. Sem separador, sem traversal, sem vazio."""
    if not name or not name.strip():
        raise LayoutError("nome de arquivo vazio")
    if name != name.strip():
        raise LayoutError(f"nome de arquivo com espaco nas bordas: {name!r}")
    if os.sep in name or (os.altsep and os.altsep in name) or "/" in name or "\\" in name:
        raise LayoutError(f"nome de arquivo nao pode conter separador de path: {name!r}")
    if name in {os.curdir, os.pardir} or Path(name).is_absolute():
        raise LayoutError(f"nome de arquivo invalido: {name!r}")
    return name


@dataclass(frozen=True)
class StackLayout:
    """Paths da stack instalada. Imutavel e sem side effect na construcao."""

    root: Path

    # --- construcao --------------------------------------------------------

    @classmethod
    def from_root(cls, root: str | os.PathLike[str]) -> StackLayout:
        """Layout com root explicito. Nao acrescenta nada ao path recebido.

        `StackLayout.from_root(Path("D:/AI"))` enraiza exatamente em D:\\AI.
        """
        return cls(root=_normalize_root(Path(root)))

    @classmethod
    def default(cls, env: Mapping[str, str] | None = None) -> StackLayout:
        return cls(root=default_root(env))

    # --- diretorios de primeiro nivel --------------------------------------

    @property
    def apps(self) -> Path:
        return self.root / "apps"

    @property
    def models(self) -> Path:
        return self.root / "models"

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def cache(self) -> Path:
        return self.root / "cache"

    @property
    def downloads(self) -> Path:
        return self.root / "downloads"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def runtime(self) -> Path:
        return self.root / "runtime"

    @property
    def state(self) -> Path:
        return self.root / "state"

    # --- aplicacoes --------------------------------------------------------

    @property
    def ollama_app(self) -> Path:
        return self.apps / "ollama"

    @property
    def open_webui_app(self) -> Path:
        return self.apps / "open-webui"

    @property
    def comfyui_app(self) -> Path:
        return self.apps / "comfyui"

    # --- dados por aplicacao -----------------------------------------------

    @property
    def open_webui_data(self) -> Path:
        return self.data / "open-webui"

    @property
    def comfyui_data(self) -> Path:
        return self.data / "comfyui"

    # --- derivacoes de arquivo ---------------------------------------------
    # Apenas paths. Download parcial, lock e state sao subsistemas de outra
    # etapa; aqui so se decide onde eles vao morar.

    def download_part(self, name: str) -> Path:
        """Destino temporario de download. O rename atomico e do downloader."""
        return self.downloads / f"{_safe_name(name)}.part"

    def lock_file(self, name: str) -> Path:
        return self.runtime / f"{_safe_name(name)}.lock"

    def pid_file(self, name: str) -> Path:
        return self.runtime / f"{_safe_name(name)}.pid"

    def log_file(self, name: str) -> Path:
        return self.logs / f"{_safe_name(name)}.log"

    def state_file(self, name: str) -> Path:
        return self.state / f"{_safe_name(name)}.json"

    # --- introspeccao ------------------------------------------------------

    @property
    def all_directories(self) -> tuple[Path, ...]:
        """Todos os diretorios gerenciados, pais antes dos filhos."""
        return (
            self.root,
            self.apps,
            self.models,
            self.data,
            self.cache,
            self.downloads,
            self.logs,
            self.runtime,
            self.state,
            self.ollama_app,
            self.open_webui_app,
            self.comfyui_app,
            self.open_webui_data,
            self.comfyui_data,
        )

    def as_dict(self) -> dict[str, Path]:
        """Nome conceitual -> path. Util para doctor e para o install plan."""
        names = (
            "root",
            "apps",
            "models",
            "data",
            "cache",
            "downloads",
            "logs",
            "runtime",
            "state",
            "ollama_app",
            "open_webui_app",
            "comfyui_app",
            "open_webui_data",
            "comfyui_data",
        )
        return {name: getattr(self, name) for name in names}

    def exists(self) -> bool:
        """True se todos os diretorios gerenciados ja existem."""
        return all(path.is_dir() for path in self.all_directories)

    def missing_directories(self) -> tuple[Path, ...]:
        return tuple(path for path in self.all_directories if not path.is_dir())

    # --- criacao -----------------------------------------------------------

    def conflicts(self) -> tuple[Path, ...]:
        """Paths gerenciados que ja existem como arquivo em vez de diretorio."""
        return tuple(p for p in self.all_directories if p.exists() and not p.is_dir())

    def ensure(self, *, dry_run: bool = False) -> tuple[Path, ...]:
        """Cria os diretorios que faltam. Idempotente.

        Devolve o que foi criado (ou o que seria criado, com dry_run=True).
        Nunca apaga, nunca sobrescreve arquivo. Valida tudo antes de criar
        qualquer coisa, para nao deixar layout pela metade.
        """
        conflicting = self.conflicts()
        if conflicting:
            listed = ", ".join(str(p) for p in conflicting)
            raise LayoutError(
                f"esperava diretorio mas encontrou arquivo: {listed}. "
                "Nada foi criado ou removido; resolva o conflito manualmente."
            )

        missing = self.missing_directories()
        if dry_run:
            return missing

        created: list[Path] = []
        for path in missing:
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise LayoutError(f"nao foi possivel criar {path}: {exc}") from exc
            created.append(path)
        return tuple(created)
