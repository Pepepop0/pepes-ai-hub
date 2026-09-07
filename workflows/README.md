# Workflows

Workflows **ComfyUI API format**, não o JSON visual comum.

- `flux2-klein-t2i-api.json` — exportado do ComfyUI Desktop desta máquina
  (FLUX.2 Klein text-to-image). Versionado byte a byte como saiu do export.
  O mapping dos node ids está em `manifests/workflows.yaml` e é validado
  contra este arquivo pelos testes.
- `flux2-klein-edit-api.json` — ainda não versionado. O export existe, mas o
  grafo de edição não tem nós de largura/altura (vêm de `GetImageSize`) e
  carrega dois `LoadImage`, então o mapping precisa de uma decisão antes de
  entrar.

O valores gravados no export (prompt, seed, nome do modelo) são apenas o
estado em que o grafo foi salvo: `render_workflow` sobrescreve todos eles.
