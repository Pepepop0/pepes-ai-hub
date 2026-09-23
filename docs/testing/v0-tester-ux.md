# The v0 tester experience: what changed, and what was exercised

The audit's findings for v0, as implemented, and the evidence. Nothing about
chat, image generation, image editing, tools, vision or the service lifecycle
was touched: this is the Hub around them.

## The blocker: which installation a command reaches

Choosing another profile or folder bound the session to it **before** the
attempt was checked. A conflict the person then cancelled left the Hub
pointing at an installation the Dashboard was not showing, and the next
Start, Stop or Restart went there.

Now a selection prepares a *candidate* -- its own backend, its own
configuration -- and only a completed installation or an approved switch
commits it. Until then the bound installation is the one shown and the one
commanded. `tests/test_installation_switching.py` holds the proofs: a
cancelled conflict, a selection that cannot be prepared and an installation
that fails all leave the previous binding, and the active pointer on disk is
written only after a run succeeds. Operation events are published from the
session that produced them, so a candidate's installation is observed while
the bound one stays put.

## State: global, per installation, derived

| what | where | example |
| --- | --- | --- |
| global to the Hub | `state/hub-selection.yaml` (schema 2) | active installation, known installations, last used, last install/repair/failure, interface preferences |
| per installation | `<root>/state/config.yaml` (schema 1, unchanged) | profile, stack, mode |
| derived, never stored | observed on demand | running, ready, folder exists, free space |

Schema 1 is migrated on load and only written back in the new shape when
something changes; the machine's own file was migrated with PEPEH intact
(`tests/test_installation_registry.py`, and the real file on this machine).
A folder is identified by its normalized path, so the same folder written
with another case, separator or trailing slash is one entry. Portable keeps
its precedence and is registered under the folder it resolved to.

## The Installation screen

Five things the mission asked for, all present:

1. **quick switch** -- the known installations that can be used, the current
   one excluded;
2. **detailed list** -- every installation with its observed state (ready,
   running, stopped, folder missing, configuration unreadable), its folder,
   and per-row actions;
3. **Windows folder picker** -- "Abrir instalação existente" and "Localizar
   pasta" for an entry whose folder moved;
4. **Trocar instalação** -- an explicit action, not a side effect of editing
   a field;
5. **Nova instalação** -- which never replaces the one in use; if it fails,
   the previous installation is still active.

Forgetting removes the Hub's shortcut and no files, says so on screen, and
refuses to forget the installation in use. Switching while a stack runs asks
first -- "Escolha a mesma instalação novamente para parar e trocar" -- and only
then stops the services this Hub can prove it owns; external processes are
never touched, and if stopping fails the previous installation stays active.

"Reinstalar" became **Verificar e reparar**: the idempotent first run on the
root, profile and mode already chosen.

## Dashboard, review, leaving, small terminals

- The Dashboard names the installation it is about -- profile, folder, how it
  was installed -- and offers what applies: a stopped stack is started, a
  running one opened, restarted or stopped.
- The review before installing says what will be downloaded and how much room
  the disk has ("at least" when a size is unknown, nothing when it cannot be
  known) and no longer calls tens of gigabytes "a few minutes". The figures
  come from the manifests (every pinned model now records its size) and from
  the volume the folder would be created in, without creating it.
- Closing the Hub does not stop the stack, and says so; while an operation is
  running it refuses to close, because this version cannot cancel one safely.
- At 80x24 the rail is not drawn and its pet does not animate; below 120
  columns it goes narrow; nothing critical lives only there.
- The profile to start on is the largest one the detected VRAM can run, and a
  profile that asks for more VRAM than the machine has cannot be carried into
  an installation. The Test profile declares `visibility: internal` and is not
  offered (`PEPITOS_AI_HUB_INTERNAL_PROFILES=1` brings it back); no screen
  reads a profile id.
- One language on screen, no emoji, technical ids in details.

## What the real machine showed

Run against the three real installations (`PEPEH`, `PEPITO_8GB_TEST`,
`CAPELLA_TEST`), nothing installed or deleted:

- the Dashboard had no folder to show, because the installation view was
  built without one -- fixed;
- `PEPITO_8GB_TEST` and `CAPELLA_TEST` had no `state/config.yaml` (they were
  installed by a script, not by the Hub), and both were refused with the same
  sentence as a folder of photos. A folder that holds a stack but no
  configuration now says exactly that. The two were given the configuration
  that matches what is installed in them, and were then adopted, switched
  between, forgotten and re-adopted -- with their files untouched;
- `CAPELLA_TEST/state/config.yaml` had been overwritten by the old adapter
  with the *active* profile (`pepe`) rather than its own: the blocker this
  work fixes, seen in the wild. The stale registry row was forgotten; the
  file was replaced by the correct one (`capella`), and the wrong one is kept
  beside it as `config.yaml.wrong-profile.bak`;
- with a stack running, every switch asks to stop first -- ports are ports,
  whoever owns them -- and with the ports free the switch, the forget, the
  refusal to forget the one in use, and the re-adoption all behaved as
  designed, surviving a restart of the Hub with the list and the selection
  intact.

The frozen build was run for real against PEPEH: it opened, launched Open
WebUI and quit on `q` with no traceback and no process left behind.

## Not in this version

Uninstall, two stacks at once, dynamic ports, cooperative cancellation,
auto-update, installer/MSI, signing, drive scanning, UUID identity for
installations.
