# Contrato IC5 Models → IC6 Config/Workflows

Este documento congela apenas a fronteira entre as duas fases. Não define um
catálogo real, não baixa modelos e não configura workflows.

## Identidade

`ModelId` é o único identificador de modelo compartilhado entre profile,
catálogo, resolver e workflow. O formato é:

```text
<provider>/<stable-name>
```

Providers atuais: `ollama` e `comfyui`. Exemplos:

```text
ollama/hermes3_8b
comfyui/flux2_klein_fp8
comfyui/qwen_3_4b_encoder
comfyui/flux2_vae
```

O provider faz parte da identidade porque define lifecycle e forma de consumo.
`kind` não faz parte: corrigir a classificação de um item não deve renomeá-lo.
Nome/tag do Ollama, filename e URL são detalhes mutáveis do `ModelSpec`, não IDs.

Aliases legados pertencem exclusivamente ao loader do catálogo de IC5. Ele pode
aceitar aliases declarados no manifest e deve convertê-los para `ModelId` antes
de devolver qualquer objeto público. Não existe tabela de aliases em profiles,
workflows, runtime ou TUI. Os três profiles do repositório já usam IDs canônicos.

## Declaração do catálogo

`ModelSpec` contém `id`, `kind` e uma source discriminada:

- `OllamaModelSource`: reference de pull/status e digest opcional;
- `FileModelSource`: um `Artifact` existente (URL, filename, version e SHA-256)
  mais `ModelPlacement`.

`Artifact` continua sendo a fonte única para bytes verificáveis. `ModelSpec`
acrescenta semântica de modelo; não duplica downloader ou checksum.

## Resultado entregue a IC6

IC5 implementa o protocolo `ModelResolver`:

```python
resolve(model_id: ModelId) -> ResolvedModel
```

`ResolvedModel` carrega identidade, kind, estado e, somente quando `AVAILABLE`,
uma location discriminada:

- `OllamaModelLocation(reference)`, sem path artificial;
- `FileModelLocation(path absoluto, runtime_name, placement)`.

`require_location()` falha para `NOT_INSTALLED`, `INVALID` e `EXTERNAL`.
Assim IC6 não consegue transformar presença parcial ou instalação externa em
configuração controlada por acidente. `AVAILABLE` significa que IC5 já comprovou
o provider reference ou a integridade do arquivo.

## Ownership de paths

`ManagedModelPaths` é a única política de placement de arquivos managed:

```text
<StackLayout.models>/comfyui/<placement>/<filename>
```

IC5 baixa diretamente para esse destino usando staging/rename do downloader.
Não há cópia, hardlink, symlink ou segunda árvore canônica neste contrato. IC6
recebe o path e `runtime_name` resolvidos e configura o ComfyUI para enxergar a
árvore; não remonta paths. Ollama mantém seus modelos pelo próprio provider e é
representado por reference, não por arquivos descobertos pela aplicação.

Construir `ManagedModelPaths` é read-only; criar diretórios pertence ao apply de
IC5. `StackLayout` continua sem conhecer profiles, catálogo ou workflows.

## Profiles e workflows

O formato atual dos profiles permanece. Valores selecionados em `primary`,
`diffusion`, `text_encoder`, `vae` e `alternatives` são `ModelId`; `TBD` significa
seleção ainda não resolvida. `Profile.model_requirements` expõe somente escolhas
ativas. Alternativas são IDs válidos, mas não são adquiridas até serem escolhidas.

Workflow manifests referenciam `model_id`. Durante geração/configuração, IC6
recebe `ResolvedModel`, chama `require_location()` e usa:

- `OllamaModelLocation.reference` em configuração de provider;
- `FileModelLocation.runtime_name` nos nodes e o path já resolvido na
  configuração de search paths.

IC6 não importa catálogo, downloader, URLs, hashes ou lógica de instalação.

## Estados

| Estado | Significado | Consumível por IC6 |
|---|---|---|
| `NOT_INSTALLED` | catálogo conhecido, modelo ausente | não |
| `AVAILABLE` | referência/bytes verificados por IC5 | sim |
| `INVALID` | presença parcial ou integridade divergente | não |
| `EXTERNAL` | encontrado fora da ownership managed | não |

O enum é específico de models. Não reutiliza `ComponentState`, pois instalação
de serviço e disponibilidade de modelo têm lifecycles diferentes.

## Fronteiras de ownership

| Camada | Possui | Não possui |
|---|---|---|
| Catalog | aliases, `ModelId → ModelSpec`, pins e metadata | download, paths absolutos, workflow |
| Profile | intenção e roles ligados a `ModelId` | URL, hash, filename, install path |
| Model installer/resolver | pull/download, integridade, estado, placement e `ResolvedModel` | configuração de serviços/workflows |
| Config/workflow | materialização de config a partir de `ResolvedModel AVAILABLE` | catálogo, download, hash, descoberta de arquivos |
| Provisioning | binários/ambientes dos três componentes | modelos e workflows |
| Runtime | processos, health, logs e adapters | instalação, resolução ou placement de modelos |

## Testes de fronteira obrigatórios

IC5 deve provar:

- todo ModelId do profile resolve para exatamente um ModelSpec;
- aliases são normalizados somente no ingresso do catálogo;
- arquivo instalado e íntegro produz `FileModelLocation` absoluta;
- Ollama presente produz `OllamaModelLocation` sem path;
- missing, invalid e external nunca expõem location;
- placement vem exclusivamente de `ManagedModelPaths`;
- apply é idempotente e dry-run não muta.

IC6 deve provar:

- workflow/config aceita apenas `ResolvedModel AVAILABLE`;
- node usa `runtime_name` e provider usa `reference` recebidos;
- nenhum módulo de IC6 importa catalog ou downloader;
- workflows usam os mesmos ModelIds de profile e resolver;
- nenhum path é reconstruído ou descoberto por filename.

Testes de integração posteriores devem conectar os dois lados por
`ModelResolver`, sem mocks de URL/path dentro de IC6.
