# Building the Windows release

This is for whoever cuts a release. A person using the Hub never needs it:
they get `PepitosAIHub.exe`, double-click it, and the first-run wizard takes
over.

## Prerequisites

- Windows 10/11, x64.
- This checkout bootstrapped: `bootstrap-dev.bat` (creates `.venv` on
  Python 3.11).
- Network access once, for the build dependency: the script installs
  `pyinstaller==6.22.3` (pinned in `pyproject.toml` under `[build]`) into
  `.venv`.

Nothing else. No Python needs to be on the target machine: the product
provisions its own (see *Managed Python*).

## Command

```powershell
.\scripts\build_windows.ps1
```

Options: `-SkipSmoke` builds without running the packaged self-check;
`-Version` prints the version the build will carry and exits.

The script:

1. installs the pinned PyInstaller into `.venv`;
2. removes only its own previous output (`dist\PepitosAIHub` and
   `build\pepitos_ai_hub`), never `dist\` or `build\` wholesale;
3. runs PyInstaller with `packaging\pepitos_ai_hub.spec`;
4. verifies `PepitosAIHub.exe` exists and that `profiles\`, `manifests\`,
   `workflows\` and `assets\` were bundled with their sentinel files;
5. runs `PepitosAIHub.exe --smoke` and `--version` on the result.

## Output

```
dist\PepitosAIHub\
    PepitosAIHub.exe        the Hub
    _internal\              Python runtime, libraries, and the product's data:
        profiles\  manifests\  workflows\  assets\
```

About 24 MB. Ship the whole folder; the executable does not run without
`_internal`. `dist\` and `build\` are ignored by git - binaries are never
committed.

## How to run it

Double-click `PepitosAIHub.exe`, or run it from a terminal. The console window
that opens **is** the Hub: it is a terminal application, so it needs a console
host to draw in. That window is not an extra helper. The services it starts
(Ollama, Open WebUI, ComfyUI, and pip during installation) run with
`CREATE_NO_WINDOW` and never open windows of their own.

`PepitosAIHub.exe --smoke` prints a JSON report of what the build can see and
exits 0 when everything resolves. `--version` and `--doctor` work as they do
on a checkout.

## What the build changes about behaviour

**Resources.** `local_ai_stack.resources` answers "where is the product's
data?" - the checkout root when run from source, the bundle's `_internal`
when frozen (`sys._MEIPASS`). Nothing depends on the working directory.
`PEPITOS_AI_HUB_RESOURCES` overrides it.

**State.** Unchanged: `%LOCALAPPDATA%\PepitosAIHub\state` for the default
install, and the selection file lives there too. The build never writes
inside `_internal`.

**Portable.** "Beside the Hub" now means beside the executable:
`dist\PepitosAIHub\stack\`. That is the folder to copy to another disk. It is
next to `_internal`, never inside it.

**Managed Python.** A frozen build has no Python to create environments from -
its `sys.executable` is the Hub itself, and `find_interpreter` refuses to offer
it. Instead the product provisions the interpreter pinned in
`manifests/artifacts.yaml` (`python-windows-amd64`: python-build-standalone
CPython 3.11.16, SHA-256 verified) into `<root>\runtime\python` the first time
Open WebUI or ComfyUI needs one, under the same marker discipline as Ollama.
Both components share it. A source checkout keeps using its own `.venv`
Python exactly as before; the choice is made once, in
`integration.default_provisioners`, from whether the process is frozen.

**Folder picker.** The native dialog (`folder_picker.py`, ctypes/COM) is
bundled; `--smoke` proves it imports. Whether the window actually opens is
part of the manual smoke below.

## Known limitations

- Unsigned. SmartScreen will warn on first run of a downloaded copy; that is
  expected for v0.1 and is not addressed here (no signing, no installer, no
  MSI, no winget, no auto-update).
- One-directory build by design: `PepitosAIHub.exe` must stay with its
  `_internal` folder. A single-file build was rejected - slower start,
  self-extracting behaviour antivirus dislikes, and bundled data hidden in a
  temp directory.
- The Hub is a console application. Launching it from Explorer opens a console
  host; there is no windowed mode.
- Windows/NVIDIA only, matching the product's v0.1 target. The build does not
  run elsewhere and does not try to.
- The managed Python is downloaded on first install (~48 MB), the same way
  Ollama and the models are.

## Manual smoke procedure

Automated checks cover imports, resources, paths and the interpreter
decision. These need a person:

1. Copy `dist\PepitosAIHub\` somewhere that is not the checkout (for example
   `C:\Temp\PepitosAIHub`). Double-click `PepitosAIHub.exe`. Expect the
   first-run wizard in a console window, no traceback.
2. Welcome -> Perfil -> Local: choose **Personalizado**. Press
   **Escolher pasta...**: the Windows folder dialog must open. Cancel; the
   field must be unchanged. Press it again, pick a folder; the field must show
   it and **Continuar** must enable.
3. Review shows the profile and the exact folder. Press **Instalar**.
4. Watch the install pane. Under Open WebUI or ComfyUI (whichever comes first)
   expect "Preparing the managed Python 3.11.16" then a download of
   `cpython-3.11.16+…-install_only.tar.gz`. Confirm
   `<root>\runtime\python\python.exe` appears with a `.pepito-managed.json`
   marker beside it, and that no `PepitosAIHub.exe -m venv` ever shows up in
   the details.
5. Confirm no extra console windows flash while Ollama, Open WebUI and
   ComfyUI start.
6. **Abrir Open WebUI** must open the browser on the managed endpoint.
7. Close the Hub, reopen it: it must go straight to the Dashboard.
8. Right-click `PepitosAIHub.exe` -> Properties -> Details: version 0.1.0,
   product name "Pepito's AI Hub".
