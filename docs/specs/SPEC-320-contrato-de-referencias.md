# SPEC-320 — Contrato de referências para qualquer escritor e lab de 2 a 3 fontes (LAB-04)

> Registro técnico SFW. As referências históricas a `edit_lab.py`, seus testes e fontes experimentais pertencem ao pacote privado e não são comandos ou fixtures disponíveis no público. A implementação pública de métricas e avaliação compartilhadas é `scripts/lab_common.py` / `scripts/lab_review.html`.

| Campo | Valor |
|---|---|
| Onda | 3 (pedido do usuário, 15/11) |
| Cobre | CHAT-15, LAB-04 (preparação), parte do CHAT-14 |
| Prioridade / esforço | P2 / G |
| Depende de | SPEC-319 (par E/C, filtro 0.26) |
| Fonte da verdade técnica | `docs/research/2026-11-15-referencias-multiplas.md` (leia inteira; cita arquivo:linha e fontes externas) |

## Pedido

Implementar a recomendação A+B+C da pesquisa e deixar o LAB-04 pronto para rodar num pod:

- **A.** O prefixo de papéis e fins por slot (`reference_prompt`) passa a ser compilado para **todos os escritores assistidos** (`router`, `vision`, `vision_think`, E e C), depois do texto autoral e antes da salvaguarda e da persistência. `raw` continua literalmente cru (controle diagnóstico, documentado). Uma fonte mantém o `Source purpose` de hoje.
- **B.** Os escritores que olham imagens (`prompt_writer_payload`, `prompt_revision_payload`, `frozen_route_payload`, `write_prompt_pair`) recebem **todas** as fontes, na ordem física dos slots, e um **mapa explícito** número-do-chat → posição-física, com papéis e fins de cada slot. A imagem da área anotada é um campo à parte, nunca um slot de geração.
- **C.** Convenção por família: Qwen 2.1 usa `<imageN>` (N = posição física depois de ordenar os slots); Krea usa ordinais, cena primeiro e personagem depois. Nunca usar o número do chat como índice físico.
- **Lab.** O `scripts/edit_lab.py` monta o grafo `edit_refs` de N fontes (2 e 3 no Qwen, 2 no Krea) a partir do manifesto/stack, para o LAB-04 da seção 5 da pesquisa.

## Fora do escopo (não fazer)

- Ampliar o par E/C para 2+ fontes (passo 6 da pesquisa): só depois do LAB-04.
- Mudar capacidades configuradas (Qwen 1–3, Krea 1–2), canvas, `ref_boost`, `grounding_px` ou os workflows.
- Subir a versão do filtro (o integrador faz) e rodar GPU.
- Refatorar o `_routed` além do necessário: mudanças pequenas e locais, filtro continua um arquivo autocontido.

## Contrato

1. **Mapa de slots.** `_routed` resolve a lista de slots uma vez (`reference_plan`) e produz, para cada slot: `slot_index` (1-based, ordem física), `chat_number`, `roles`, `purposes`, `is_canvas` (a base). O mapa alimenta escritores, compilador e receita.
2. **Compilador único.** Uma função (ex.: `compile_reference_prompt(slots, syntax, authored)`) aplica o envelope depois do texto autoral, idempotente: se o texto já traz o envelope ou tags, não duplica. Teste de idempotência no ponto de entrada; nada de heurística que apague trechos parecidos.
3. **Escritores com N fontes.** Os payloads aceitam uma lista ordenada de imagens e o mapa; o prompt de sistema diz qual imagem é cada slot e proíbe renumerar ou redefinir papéis. Atualizar todos os chamadores no filtro **e** no lab, sem shim monoimagem. Com uma fonte, o comportamento e o texto atuais ficam iguais (os testes atuais continuam verdes).
4. **Receita e regeração.** Receita grava o prompt final já compilado e as fontes em ordem. Regeração exata, "mesma semente", "refazer na imagem inteira" e "tente de novo" usam o prompt final gravado, sem recompilar nem rerotear; teste de idempotência.
5. **Lab (N fontes).** `prompt_prepare` aceita `sources[]` ordenadas (`chat_number`, `roles`, `purposes`, `is_canvas`, tamanho e hash); `prompt_request` envia todas as fontes e o mapa; `prompt_graph` escolhe o workflow por família/manifesto e N (o mesmo `edit_refs[N]` do filtro), preenche os LoadImage de cada slot e recusa mapeamento incompleto (sem fallback monoimagem); `prompt_run` faz o upload de todas as fontes com a mesma geometria do filtro (`model_edit_sources`/`5636-5666` da pesquisa) e agrupa as execuções por modelo real. O `redraw_graph` continua monoimagem. O lab continua sem importar o pacote Hub nem Pydantic.
6. **Variantes do LAB-04** (seção 5.3 da pesquisa): V0 router com envelope, V1 `vision` sem envelope (controle), V2 mesmo texto de V1 com envelope, V3 E com todas as fontes e mapa, V4 C congelado com todas as fontes e mapa, V5 (só Qwen) V3 com ordinais no lugar de `<imageN>`. Prompts escritos uma vez por combinação e reutilizados nas duas sementes; decisão, slots, canvas, força e sementes congelados. Gravar decisão, mapa, hash das fontes, prompt autoral, envelope, prompt final, request e answer completos, grafo, semente, família, tempos e a saída bruta.
7. **Casos.** Gerar `cases.json` do LAB-04 com R1–R8 da pesquisa (seção 5.2), usando os arquivos nomeados de `$HOME/Downloads/pod-onda-4/lab-prompt-2/sources` e `$HOME/Downloads/swap-lab/inputs`. Esse conjunto de fontes é privado e não deve ser incluído no público. Para regressões públicas, use imagens sintéticas e pedidos SFW; a adequação visual das fontes privadas fica a cargo do dono.
8. **Avaliador.** `write_prompt_review` do lab ganha as legendas de referências (papel e índice) e variantes com rótulo neutro; o resto do avaliador não muda.

## Critérios de aceite

1. Testes do filtro: com 2 e 3 fontes, base fora de ordem, A+B+C, A+B no mesmo slot e referência duplicada, cada escritor assistido entrega ao workflow o prompt com o envelope correto (Qwen `<imageN>` pela posição física; Krea ordinais), uma vez só; `raw` cru; uma fonte inalterada.
2. Os payloads dos escritores levam N imagens na ordem dos slots mais o mapa; a imagem da área não conta como fonte; a decisão do roteador não muda.
3. Receita e regeração: idempotência provada (prompt final gravado intacto) com N fontes.
4. Lab: dry-run do LAB-04 monta, sem serviço nenhum, os grafos de 2 e 3 fontes (Qwen) e 2 fontes (Krea) com os LoadImage na ordem, seed e canvas certos, e recusa grafo/mapa incompleto; pedidos e grafos reais gravados; nenhuma resposta de modelo inventada.
5. roteiro histórico da Onda 5 (pacote privado): roteiro só do lab (sem interface, sem conta), com comandos, o que medir, a regra de decisão da pesquisa (5.5) e a contagem de imagens; pacote do lab em `$HOME/Downloads/pod-onda-5/lab-referencias` (`cases.json` e `sources/`).
6. `ruff format`/`check` limpos; testes dos arquivos afetados verdes; suíte completa rodada pelo integrador.

## Notas da implementação

- `reference_plan` resolve e deduplica uma vez; cada slot preserva o número do chat e ganha `slot_index`, `chat_number` e `is_canvas`. Escritores, compilador e receita compartilham esses mesmos slots. Papéis/purposes agregados não mudam a ordenação nem as capacidades configuradas.
- `compile_reference_prompt` reutiliza `reference_prompt` para router/vision/vision_think e os dois escritores do par. O reconhecimento de um envelope completo é exato e idempotente; não apaga nem reescreve prosa/tags autorais por aproximação. Referências isoladas no texto não substituem o contrato completo. Qwen usa índices físicos, Krea ordinais. `raw` não recebe envelope.
- **Compatibilidade monoimagem:** o compilador é neutro com uma fonte; conserva-se o `Source purpose` do router e o texto dos demais escritores/par exatamente como antes. O par produtivo continua exigindo uma fonte. As assinaturas dos payloads e do par, porém, são N-fontes, sem shim monoimagem; todos os chamadores do filtro/lab foram migrados. A cópia anotada da área tem `area_context` próprio, fora do mapa de geração, e a instrução respeita `area_paste=False`.
- O prompt final é gravado na receita; regeração igual/mesma semente/saída inteira não chama escritor nem compilador. O retry multirreferência puro também reutiliza o final gravado; comandos inequívocos de repetição não reroteiam. Pedidos com alterações novas continuam no planejador existente. Uma fonte mantém inclusive a política de repetir o par E/C.
- O lab prepara um JSON portátil com todas as fontes, hashes/bytes, mapa e workflow/mapa do manifesto resolvido pelo perfil/família e N. Não há fallback de grafo multirreferência para monoimagem. O preparo de traces usa família registrada/stack/modelo do perfil, não confunde `edit_method=native` com Krea; sem proveniência exige família explícita nos fatos. O trace de novos jobs também registra família.
- V0–V5 congelam a decisão e reutilizam V1 em V2 e V3 em V5, nas duas sementes. O runner grava decisão, mapa, proveniência do workflow/software, textos autoral/envelope/final, requests, respostas completas (também antes de validar respostas inválidas), grafos, uploads, seeds, família/modelo e tempos; agrupa sampling pelo modelo real. O upload Qwen usa os mesmos helpers geométricos de produção. `redraw_graph` segue monoimagem.
- O avaliador existente recebe legendas das fontes e rótulos neutros, sem mudança de layout/interação. Uma pasta de resultados não pode ser reutilizada com outro documento de casos congelados, evitando mistura de avaliações.
- Pacote R1–R8: `$HOME/Downloads/pod-onda-5/lab-referencias`, com 13 originais copiados por nome, declaração reproduzível, `cases.json` e dry-run. **Escolha experimental explícita:** canvas 1024×1024 e sementes 320101/320102, iguais entre variantes/famílias e R1/R2; forças/grounding/workflows não foram alterados. Não se inspecionaram imagens ou suas dimensões visuais; bytes e SHA-256 identificam os originais. A adequação visual dos pares, especialmente R5/R7/R8, continua a cargo do dono antes de GPU, como solicitado na pesquisa.
- Dry-run não simula respostas de escritor nem imagens: monta grafos apenas quando há prompt real/declarado (V0), e deixa as outras variantes aguardando respostas reais. A validação de todos os mapas/grafos ocorre antes de serviços. O roteiro completo, contagens e gate de decisão estão em roteiro histórico da Onda 5 (pacote privado).
- Sem aumento de versão do filtro, serviços, chamadas de modelos ou GPU. Não há conclusão de qualidade visual nem autorização para habilitar o par multirreferência. A suíte completa fica com o integrador.
- Verificação local em 15/11: `ruff format`/`ruff check` dos seis arquivos Python tocados limpos; **405 testes** nos sete arquivos de filtro/lab/workflows/router e **56 testes** em `test_prompt_pair.py`/`test_prompt_selector.py` (venv nativo), todos verdes. Apenas aviso de depreciação Starlette/httpx no ambiente nativo. Dry-run real do pacote: **12 combinações, 136 registros dry_run, 36 requests, 24 grafos V0 conferidos, zero falhas/respostas inventadas**, e R1/R2 equivalentes por família/seed (exceto nomes de upload/prefixo). Relatório: `lab-referencias/dry-run-verification.json`. Critérios 1–5 atendidos localmente; critério 6: gates focados atendidos, suíte completa reservada ao integrador.
