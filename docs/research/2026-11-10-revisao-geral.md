# Relatório 2 — Revisão geral do projeto

> Registro técnico SFW. As referências históricas a `edit_lab.py`, seus testes e fontes experimentais pertencem ao pacote privado e não são comandos ou fixtures disponíveis no público. A implementação pública de métricas e avaliação compartilhadas é `scripts/lab_common.py` / `scripts/lab_review.html`.

Data: 10/11/2026. Base: `test/all-in-one` em `049a8a2` mais as mudanças sem commit. Somente leitura.

## Alcance real desta revisão (leia antes)

Planejei nove frentes em paralelo com subagentes. **Todos os nove caíram por limite de taxa da conta (erro 429) com 1 a 6 minutos de trabalho e não entregaram nada.** Fiz a revisão sozinho, em uma passada. Consequência:

| Área | Profundidade |
|---|---|
| Pipeline de imagem no chat (`assets/open-webui`) | Boa: li os trechos do roteador, decisão, preservação, correção e os traces |
| Desempenho medido pelo trace | Boa para a sessão de 09/11; nas outras três só contei falhas do roteador, recusas e latência |
| Análise estática (ruff, mypy, tsc, npm audit) | Feita, números abaixo |
| Núcleo do Hub (`src/local_ai_stack`, 129 arquivos) | **Rasa**: estatísticas e buscas por padrão; não li os módulos de ponta a ponta |
| Segurança | **Parcial**: servidor desktop, link de convidado, leitura de arquivos no filtro. Não auditei downloads, exclusão, exportação de sessão, XSS no JS |
| Frontend React | **Rasa**: compilação, auditoria de dependências e busca por temporizadores |
| Script Linux (`capella-linux.sh`) | Não revisado |
| Referências externas | **Fraca**: poucas fontes abertas; o que vem de memória está marcado [NÃO VERIFICADO] |

O que está aqui é confiável dentro desse alcance. O que falta está listado no fim, para uma segunda rodada.

## Retrato do projeto

| Medida | Valor |
|---|---|
| Código (Python, JS, TS, scripts rastreados) | 66.752 linhas |
| Maior arquivo | `assets/open-webui/pepito_image_chat.py`: 7.906 linhas |
| Núcleo do Hub | 129 arquivos em `src/local_ai_stack` |
| Arquivos de teste | 160 |
| Frontend | 23 arquivos em `desktop-ui/src`; bundle JS de 504 KB, CSS de 46 KB |
| Ramos locais | 24, mais 8 worktrees |
| `dist/` | 43 itens, 749 MB (não rastreado) |
| Versão | 1.19.0 em três lugares: `pyproject.toml`, `__init__.py`, `desktop-ui/package.json` |

## Tabela-resumo dos achados

Severidade: Crítica / Alta / Média / Baixa. Esforço: P (horas), M (dias), G (semana ou mais).

| ID | Achado | Sev. | Esf. | Risco de regressão |
|---|---|---|---|---|
| A1 | `_routed` tem 377 instruções e complexidade 110 | Alta | G | Alto se refatorar sem rede de testes de decisão |
| A2 | Filtro de 7.906 linhas num arquivo só | Alta | G | Médio |
| P1 | Modelo de chat recarregado em todo turno de imagem (6,1 s de 27 s) | Alta | M | Baixo |
| P2 | Roteador depende do modelo de 27B residente (6,3 s por turno) | Média | M | Médio (qualidade do roteamento) |
| P3 | Carga fria de 57 s e descarga por ociosidade | Média | P | Baixo |
| P4 | Troca de personagem varia de 70 a 252 s, sem tempo por etapa no trace | Média | P (medir) | Nenhum |
| O1 | Trace não guarda semente efetiva, texto da resposta nem tempo por etapa | Média | P | Nenhum |
| Q1 | Sem integração contínua | Alta | P | Nenhum |
| Q2 | Sem arquivo de trava das dependências Python | Média | P | Baixo |
| Q3 | mypy: 225 erros em 42 arquivos; não é exigido | Média | M | Nenhum |
| Q4 | Zero testes de frontend e do JS injetado (1.464 linhas) | Média | M | Nenhum |
| Q5 | 84 `except Exception` silenciados por `noqa: BLE001` | Média | M | Baixo |
| Q6 | Código de teste (`e2e/`, `test_api/`) dentro do pacote do produto | Baixa | P | Baixo |
| D1 | Documentação com quatro versões diferentes do perfil Capella | Média | P | Nenhum |
| D2 | Documentos de agosto e setembro possivelmente órfãos | Baixa | P | Nenhum |
| D3 | Checklist descreve correção automática que não existe mais | Média | P | Nenhum |
| S1 | `picture_bytes` lê arquivo por id sem checar o dono | Baixa | P | Baixo |
| S2 | Link de convidado abre cadastro a qualquer um com o link | Média (por desenho) | P | — |
| S3 | Trace grava pedidos e prompts em texto no disco | Baixa (uso local) / Média (pod com convidado) | P | Nenhum |
| R1 | Entradas e saídas do ComfyUI nunca são limpas | Baixa | P | Baixo |
| R2 | Estado em variáveis de módulo; trava da GPU só vale num processo | Baixa | — | — |
| H1 | 24 ramos, 8 worktrees, `dist/` de 749 MB, arquivo vazio rastreado | Baixa | P | Nenhum |
| H2 | Dois ramos mantidos idênticos por cherry-pick | Baixa | P | Nenhum |

---

## 1. Arquitetura e manutenibilidade

### A1 — `_routed`: uma função decide tudo (Alta)

**Evidência:** `ruff --select C901,PLR0915`: `assets/open-webui/pepito_image_chat.py:6730` tem 377 instruções (o limite usual é 50) e complexidade ciclomática 110 (o limite usual é 10). É a função que recebe a resposta do roteador e decide chat, busca, geração, edição, parte, máscara, correção e troca.

**Impacto:** três dos problemas do relatório 1 nasceram aqui, por combinação de ramos que ninguém previu: plano completo descartado quando `action == chat` (item 1), pergunta de esclarecimento ignorada (item 1), parte `restage` com `scope: local` recusada (item 4). Cada correção acrescenta um ramo; cada ramo novo aumenta a chance do próximo caso não previsto.

**Recomendação:** antes de dividir, fixar o comportamento com uma **tabela de decisão testada**: entrada = (resposta do roteador, estado do chat), saída = (ação, parte, fonte, máscara, mensagem). O trace já guarda `raw_json` de cada resposta real; as quatro sessões dão dezenas de casos de graça. Depois, extrair em etapas puras:

```
normalizar(decisão)          # reconciliar action × plan × clarification
resolver_fontes(...)         # retry, current, referências
escolher_parte(...)          # edit / redraw / restage / remove / pose / swap
preparar_área(...)           # máscara pintada, automática, nenhuma
montar_job(...)
```

As demais funções acima do limite, para registro: `desktop/api.py:547 _execute` (136 instruções, complexidade 68), `cli.py:173 smoke_report` (102), `session_export.py:1009 verify_session_archive` (87), `profiles.py:542 validate_profile_data` (87), `service_config.py:712 open_webui_image_stacks` (76), `download.py:486 _segmented` (73). No total: 81 funções acima de complexidade 10.

### A2 — Um arquivo de 7.906 linhas (Alta)

**Evidência:** `pepito_image_chat.py` concentra roteador (prompts e esquema), salvaguarda de idade, consentimento, regras de reserva por palavras, plano e consistência, calibragem de fidelidade, registro de personagens, máscara, despacho ao ComfyUI, troca de personagem, trace e gestão de VRAM.

**Restrição conhecida:** os módulos auxiliares já existem e são importados como `open_webui.pepito_*` (por exemplo `from open_webui.pepito_image_preservation import preserve_pixels`, `:2533`). Ou seja, o mecanismo para dividir já funciona.

**Proposta de divisão**, na ordem de menor risco:

| Módulo novo | Conteúdo | Linhas aprox. hoje |
|---|---|---|
| `pepito_trace.py` | `write_trace`, `trace_event`, sanitização | 360–530 |
| `pepito_vram.py` | descarga do Ollama, `/free` do ComfyUI, trava da GPU | 535–890 |
| `pepito_router.py` | esquema, prompts, `route`, `decide`, regras por palavras | 895–1520, 1940–2230 |
| `pepito_safeguard.py` | menores e consentimento | 1520–1935 |
| `pepito_plan.py` | validação do plano, consistência, retry, fidelidade | 2740–3770 |
| `pepito_image_chat.py` | só o filtro (`inlet`, `_routed`, `outlet`) e o despacho | o resto |

Cada extração é mecânica e pode ir num commit próprio com a suíte verde. Os prompts do roteador (centenas de linhas de texto dentro do código) ganhariam um lugar onde é possível compará-los entre versões.

### Duplicação

`scripts/swap_composite.py` (317 linhas) e `assets/open-webui/pepito_swap_compose.py` (529 linhas) implementam composição da troca. [NÃO COMPARADO linha a linha.] Se o segundo substituiu o primeiro, o script de laboratório deveria importar o módulo do produto.

### Q6 — Código de teste dentro do produto (Baixa)

`src/local_ai_stack/e2e/` (9 arquivos, cerca de 4.700 linhas) e `test_api/` (9 arquivos) ficam dentro do pacote. O `.spec` do PyInstaller exclui só `tkinter, pytest, PyInstaller, ruff, mypy` (`packaging/pepitos_ai_hub.spec:52`), então esse código vai no executável [INFERÊNCIA: o PyInstaller só inclui o que é importado; não verifiquei se algo do produto importa `e2e`]. A TUI (`tui/`, cerca de 4.400 linhas, mais a dependência `textual`) também é embarcada, embora não seja o ponto de entrada padrão.

---

## 2. Desempenho e gargalos (medido na sessão de 09/11)

Fonte: `turns.jsonl`, 38 turnos, 344 eventos.

### Decomposição de um turno de edição típico (modelo Krea)

| Etapa | Tempo | Parcela |
|---|---|---|
| Roteador (modelo de 27B, residente) | 6,3 s (mediana) | 23% |
| Descarga do Ollama + job no ComfyUI (inclui carregar o modelo de imagem) | 12,5 a 14,6 s | 50% |
| Recarga do modelo de chat + frase final (15 a 21 tokens) | 6,5 a 7,8 s (6,1 s só de carga) | 27% |
| **Total** | **27,4 s (mediana de 33 turnos de imagem)** | |

| Tipo de turno | Quantidade | Mediana | Pior |
|---|---|---|---|
| Edição/geração | 33 | 27,4 s | 252 s |
| Conversa | 5 | 8,0 s | 17,6 s |
| Job `edit` (Krea, 560×848 a 1056×704) | 12 | 12,5 a 14,6 s | 35,9 s |
| Job `edit` (Qwen) | 3 | 11,7 a 11,9 s | — |
| Job `restage` | 7 | 13,2 a 14,6 s (aquecido) | 26,4 s |

### P1 — Recarga do chat para dizer "pronto" (Alta)

Detalhe e propostas no relatório 1, achado N1. É o ganho mais barato: até 7 s por turno de imagem.

### P2 — Roteador no modelo grande (Média)

O roteador usa o mesmo modelo de 27B do chat, o que obriga a alternância completa GPU ↔ imagem em todo turno. Durante os jobs do Krea sobram 14,2 GB livres na placa (`comfyui_stats.vram_free`). Um modelo de roteamento de 3 a 5 GB poderia ficar residente junto com o modelo de imagem. [INFERÊNCIA; exige medir a qualidade: o esquema do roteador é grande e o comentário em `router_messages` registra problemas com `qwen3.5:9b`.]

### P3 — Carga fria e ociosidade (Média)

- Primeiro turno: 56,8 s.
- Depois de 18 minutos parado: `ollama ps` vazio e 10,0 s. O padrão do Ollama descarrega depois de 5 minutos.
- Proposta: pré-carga na subida ([FAQ do Ollama: requisição vazia](https://docs.ollama.com/faq)) e `keep_alive` longo. Não encontrei `OLLAMA_KEEP_ALIVE` no código do Hub (busca em `src/local_ai_stack`).

### P4 — Troca de personagem: 70 a 252 s (Média)

| Turno (UTC) | Cena | Roteador | Plano por visão | Passe Qwen com marcação | Passe Krea | Total |
|---|---|---|---|---|---|---|
| 00:59 | T1 (composição em 2438×2183) | 56,8 s | ~8 s | ~119 s | ~62 s | 252 s |
| 01:36 | T1 de novo | 5,0 s | ~10 s | ~129 s | ~62 s | 210 s |
| 01:15 | T3 | 5,9 s | ~10 s | ~63 s | ~5 s | 86 s |
| 01:52 | 736×1072 | 7,0 s | ~11 s | ~49 s | ~2 s | 70 s |

Os tempos por passe foram deduzidos dos horários dos eventos `swap_canvas` e `swap_method`; cada passe inclui geração, SAM, profundidade e composição. **O trace não separa essas etapas.** Observações:

- Mesmo com a geração em tamanho reduzido, a cena grande custa o dobro no passe do Qwen (119 a 129 s contra 49 a 63 s) e 12 vezes mais no do Krea (62 s contra 2 a 5 s). O que sobra de diferente é a análise e a composição no tamanho original (5,3 MP) [INFERÊNCIA].
- Primeiro passo: registrar o tempo de cada etapa (geração, SAM, profundidade, composição) no evento `swap_method`. Sem isso qualquer otimização é chute.
- Candidatos depois de medir: rodar SAM e profundidade uma vez por cena e reaproveitar nos dois métodos; compor em resolução reduzida e ampliar só a máscara; inverter a ordem dos passes para evitar uma troca de modelo.

### ComfyUI sem opções de memória ou prévia

O Hub lança o ComfyUI só com `--listen`, `--port` e `--extra-model-paths-config` (`adapters.py:263-267`). Não usa prévia durante a amostragem nem opções de cache de modelos. Com alternância de modelos a cada turno, vale um experimento no pod com as opções de cache do ComfyUI para manter o codificador de texto em RAM. [NÃO VERIFICADO quais opções a versão fixada oferece.]

### Hub (instalação e downloads)

Só verificação por busca, sem medir:

- Downloads: retomada por `Range`, segmentos em paralelo acima de 32 MB, SHA-256 conferido, estado gravado de forma atômica (`download.py`). Bom desenho.
- Ambientes Python: instalados com `pip` (`pyenv.py:533`). O `uv` costuma ser várias vezes mais rápido em instalações grandes como a do Open WebUI; o ComfyUI Desktop adotou `uv` [NÃO VERIFICADO]. Candidato a ganho no tempo de instalação, com custo de mais um binário para fixar e verificar.

---

## 3. Observabilidade

### O1 — O que falta no trace (Média)

O trace foi o que permitiu este estudo; as lacunas custaram tempo:

| Falta | Onde fez falta |
|---|---|
| Semente efetiva de cada job (`seed: null` quando o Open WebUI sorteia) | Item 3: foi preciso ler o código do Open WebUI para descartar "semente fixa" |
| Texto da resposta do modelo de chat | Itens 1 e 5: não há como ver o que ele disse |
| Tempo por etapa (descarga, carga, amostragem, pós-processo) | P4 |
| Id do prompt no ComfyUI e nome do arquivo de saída | Casar trace com `output/` exigiu comparar pixels |
| Métricas de emenda (degrau na borda, desvio no anel) | Item 2: tive de reconstruir a máscara |
| Medida de mudança (`measured_change`) como evento | Hoje só vai para o log do Open WebUI |

### S3 — Privacidade do trace

O trace grava o texto do usuário e os prompts (até 8.000 caracteres por campo, `TRACE_TEXT_CHARS`) em `DATA_DIR/pepito-trace/turns.jsonl`, com rotação em 20 MB e uma geração anterior. Segredos e dados em base64 são removidos (`_SECRET_KEY`, `_DATA_URI`). Para uso local é aceitável. Num pod com convidados, o dono do pod lê os pedidos de todos: vale uma linha de aviso na tela de convite ou um interruptor para desligar o trace.

---

## 4. Qualidade de engenharia

### Q1 — Sem integração contínua (Alta, esforço pequeno)

Não há `.github/`. Hoje a qualidade depende de lembrar de rodar `ruff` e `pytest` antes do commit, e de manter dois ramos iguais à mão. Um fluxo mínimo no GitHub Actions (Windows): `ruff format --check`, `ruff check`, `pytest`, `npm ci` + `npm run build`. Só roda quando houver push; como a regra atual é nunca dar push sem pedido, o valor aparece quando você publicar.

### Q2 — Dependências sem trava (Média)

`pyproject.toml` usa faixas abertas (`textual>=0.80`, `httpx>=0.27`, `PyYAML>=6.0`, `psutil>=6.0`); só `pywebview`, `zstandard` e `pyinstaller` estão fixos. Não existe `uv.lock` nem `requirements.txt` com hashes. Dois builds do mesmo commit em datas diferentes podem embarcar versões diferentes. O frontend tem `package-lock.json`. Recomendo `uv lock` (ou `pip-compile --generate-hashes`) e instalar por ele no build.

### Q3 — Tipagem (Média)

`mypy src --ignore-missing-imports`: **225 erros em 42 arquivos**. O mypy está nas dependências de desenvolvimento mas não é exigido. Dois exemplos que podem ser defeito real: `desktop/agent.py:198` (chama `.release` em algo que pode ser `None`) e `desktop/__init__.py:201` (argumento de tipo errado em `start`). Recomendo zerar por módulo, começando por `desktop/`, e travar no fluxo de verificação.

### Q4 — Frontend e JS injetado sem teste (Média)

- `desktop-ui`: `tsc --noEmit` passa; nenhum arquivo de teste; sem ESLint.
- `pepito_image_ui.js` (1.464 linhas: galeria, visualizador, editor de máscara, guarda de envio): 13 menções em `tests/`, que verificam texto do arquivo ou instalação, não comportamento [INFERÊNCIA pelo tipo de busca]. O item 6 (Esc) e o item 8 (zoom) são desse arquivo.
- Recomendo testes de navegador (Playwright) contra uma página mínima que carregue o JS: abrir visualizador, zoom, Esc, pintar máscara.
- `npm audit`: 1 vulnerabilidade moderada, nenhuma alta ou crítica. Não identifiquei o pacote.

### Q5 — Exceções engolidas (Média)

84 ocorrências de `noqa: BLE001` (captura de `Exception`) e 8 de `S110` (`except: pass`) em `src` e `assets`; 12 usos de `contextlib.suppress(Exception)`. Muitas são deliberadas e comentadas ("um roteador lento é melhor que um turno falho"). O custo é diagnóstico: quando uma dessas dispara, não fica rastro. Recomendação barata: toda captura ampla registra ao menos o tipo da exceção no trace ou no log (algumas já fazem, por exemplo `:4493`).

### Suíte de testes

Medida nesta pesquisa (seção "Medição da suíte" no fim): 4.189 passaram, 85 pulados, 11 min 44 s.

Nomes por onda de entrega, não por domínio: `test_delivery_wave_a.py`, `test_final_v0_ux.py`, `test_desktop_qol.py`. Dificultam achar onde um comportamento é testado.

---

## 5. Documentação

### D1 — Quatro versões do perfil Capella nos documentos (Média)

`profiles/capella.yaml` diz **5.2.0**. Os documentos dizem:

| Documento | Versão citada |
|---|---|
| `README.md` | 5.1.0 (6 vezes) e 5.0.0 (2 vezes) |
| `docs/ARCHITECTURE.md:64` | 5.1.0 |
| `docs/testing/capella-5090-checklist.md` | 5.1.0 e 5.1.1 |
| `docs/testing/image-tool-chat.md` | 5.0.0 |
| `docs/testing/desktop-ui-checklist.md`, `runbook-linux-1.17.md` | 5.1.1 |

Recomendo tirar o número dos textos corridos (dizer "o perfil Capella") e deixar um único teste que confere a versão onde ela precisa aparecer.

### D2 — Documentos possivelmente órfãos (Baixa)

Último commit em 28/08: `docs/NIGHT_ONE_PLAN.md`, `docs/OPEN_QUESTIONS.md`, `docs/SPEC.md`, `docs/TEST_PLAN.md` (680 bytes). Em 09/09: `docs/POST_MVP_TEST_PHASE.md`. Em 12/10: `docs/CURRENT_STATE.md`. Um documento chamado "estado atual" com um mês de idade engana quem chega. Sugiro mover os históricos para `docs/historico/` e manter `CURRENT_STATE.md` ou apagá-lo.

### D3 — Checklist desatualizado (Média)

`capella-5090-checklist.md:184-186` descreve a nova tentativa automática "Quase nada mudou; refazendo com mais liberdade", que o código não faz mais. Quem seguir o checklist vai registrar uma falha que é do documento.

---

## 6. Segurança (parcial)

### O que conferi e está correto

- **Servidor do desktop** (`desktop/server.py`): escuta só em `127.0.0.1` com porta efêmera (`:47`); token aleatório de 32 bytes por processo (`:45`), comparado com `hmac.compare_digest` (`:126`); confere `Host` (`:116`), `Origin` (`:118`) e `Sec-Fetch-Site` (`:121`), o que barra DNS rebinding e requisição de outra página; cabeçalhos `Content-Security-Policy`, `X-Frame-Options: DENY`, `nosniff`.
- **Subprocessos:** o `runner.py` proíbe `shell=True` e exige lista de argumentos e tempo limite.
- **Extração de arquivos:** `ollama.py:483` recusa entrada fora do destino; `python_runtime.py:274` usa `filter="data"` e recusa links.
- **Túnel de convidado:** `cloudflared` com versão e SHA-256 fixados (`linux_share.py:38-43`); só o Open WebUI é exposto, ComfyUI e Ollama ficam no loopback.
- **Caminho nativo de imagens:** confere o dono do chat e do arquivo (`pepito_native.py:124-125, 181-182, 246-247`).
- **Escritas de estado:** quase todas por arquivo temporário + `os.replace`.

### S1 — `picture_bytes` sem checagem de dono (Baixa)

`pepito_image_chat.py:4667` e `:4686` usam `Files.get_file_by_id(id)`. Um usuário que conheça o id de um arquivo alheio poderia fazer o filtro lê-lo como fonte de uma edição (e `replace_picture`, `:4686`, grava por id). Os ids são UUID aleatórios e não são listados para outros usuários, então é improvável na prática. Correção: usar `get_file_by_id_and_user_id` com o `pepito_user_id` do turno, como o caminho nativo já faz. Só importa com mais de um usuário (link de convidado).

### S2 — Link de convidado: cadastro aberto (Média, por desenho)

`linux_share.py:45` liga `ENABLE_SIGNUP` com papel `user`. O próprio módulo avisa: "qualquer um com o link pode criar conta enquanto ele roda; feito para um pod que é apagado depois". O risco é o link vazar. Melhorias baratas: fechar o cadastro automaticamente depois do primeiro convidado (ou de N minutos); mostrar na tela quantas contas existem.

### Não auditado

Downloads e cadeia de suprimentos (pins e hashes dos manifestos, `pip` sem hashes), exclusão de instalações (junções e links simbólicos), exportação de sessão (segredos no pacote), injeção de HTML no JS e nas respostas, a salvaguarda de idade, SSRF na busca de referências, o `test_api`. O ruff aponta 17 usos de `urlopen` (S310) e 1 `tarfile` sem filtro (S202) que merecem um olhar.

---

## 7. Robustez do pipeline de imagem

- **R1 — ComfyUI acumula arquivos.** Na sessão: 50 entradas (77 MB) e 36 saídas (42 MB) em cerca de uma hora. Não achei rotina de limpeza. Num uso prolongado isso cresce sem limite; as imagens entregues já ficam nos uploads do Open WebUI. Sugestão: apagar entrada e saída do job depois de copiar o resultado.
- **R2 — Estado em variáveis de módulo.** `_GPU_GATE`, `_ACTIVE_JOBS`, `_TURN_RECORDS`, `_PICTURE_RECORDS`, `_CONSENTED` (`:383, 589, 655, 1869, 2926`). Funciona com um processo do Open WebUI. Com mais de um worker, a trava da GPU deixaria de valer. Hoje não é problema; registrar como premissa.
- **Registros JSON por chat:** escrita atômica, mas leitura-modificação-escrita sem trava (`:4058, :4769`). Dois turnos simultâneos no mesmo chat poderiam perder uma atualização. `_results_path(...).write_text` (`:5338`) não é atômico.
- **Acoplamento ao Open WebUI.** O filtro importa internos (`open_webui.routers.images`, `open_webui.models.config`, `Files`, `Storage`) e cita linhas de versões específicas nos comentários. O JS depende de ids do DOM (`#chat-input`, `#send-message-button`, `#suggestions-container`). A versão está fixada em 0.12.0, o que protege. Toda atualização do Open WebUI vai exigir uma revisão desses pontos; vale um teste que importe cada símbolo interno usado e falhe com mensagem clara.

---

## 8. Higiene do repositório

- **H1.** 24 ramos locais e 8 worktrees, vários de trabalho já integrado (`fix/…`, `feature/…`). `dist/` com 43 artefatos desde a 1.17 (749 MB). `capella_ai_config.yaml` rastreado e vazio (0 bytes). `prompts/` e `experiments/` no topo sem ligação com o produto.
- **H2.** `test/all-in-one` e `desktop-ui-shadcn` são mantidos idênticos por cherry-pick e conferência de hash. É trabalho manual em todo commit. Se os dois têm de ser iguais, um deles pode ser só um ponteiro (fast-forward) ou deixar de existir.
- O caminho do repositório tem apóstrofo (`pepito's_ai_hub`). Nesta sessão isso quebrou um comando de shell meu. Qualquer script que monte comandos por concatenação está sujeito ao mesmo.

---

## 9. Referências de outros projetos

Fraca nesta rodada; só o que consegui abrir ou ver no resultado da busca.

| Tema | Referência | O que aproveitar |
|---|---|---|
| Biblioteca de modelos compartilhada | [Stability Matrix: Shared Folders](https://docs.lykos.ai/stability-matrix/advanced/shared-folders.html): uma biblioteca única, ligada a cada pacote por link simbólico ou configuração | É a proposta P2 "cache compartilhado entre instalações" do `ROADMAP.md`; o ComfyUI já aceita `extra_model_paths`, que o Hub usa |
| Pré-carga e permanência do modelo | [FAQ do Ollama](https://docs.ollama.com/faq) | P3 |
| Recorte e costura na edição por área | [ComfyUI-Inpaint-CropAndStitch](https://comfy.org/cloud/supported-nodes/comfyui-inpaint-cropandstitch/) | Item 2, Fase 2 |
| Redimensionamento do Qwen-Edit | [ComfyUI #9481](https://github.com/Comfy-Org/ComfyUI/issues/9481), [#10198](https://github.com/Comfy-Org/ComfyUI/issues/10198) | Item 12 |
| Esc no campo de mensagem | [Open WebUI v0.12.0, `MessageInput.svelte`](https://github.com/open-webui/open-webui/blob/v0.12.0/src/lib/components/chat/MessageInput.svelte) | Item 6 |
| Semente no caminho ComfyUI | [Open WebUI v0.12.0, `utils/images/comfyui.py`](https://github.com/open-webui/open-webui/blob/v0.12.0/backend/open_webui/utils/images/comfyui.py) | Item 3 |

Não pesquisados (ficam para a segunda rodada): Pinokio, ComfyUI Desktop e comfy-cli, SwarmUI, InvokeAI, Fooocus, Krita AI Diffusion, LM Studio, Jan, SillyTavern, e a experiência de edição do ChatGPT, Gemini e Midjourney.

---

## 10. O que o projeto faz bem (para não perder)

- Trace por turno com a resposta crua do roteador: permitiu achar a causa de quatro problemas sem GPU.
- Servidor local do desktop com defesa completa contra requisição de outra origem.
- Downloads retomáveis com hash e escrita atômica de estado.
- Dependências externas fixadas onde importa (cloudflared com hash, torch no manifesto, Open WebUI 0.12.0).
- Comentários que registram a data e o motivo de cada decisão ("01/11: …"): raro e valioso.
- Compositor G2 com métricas próprias (`band_step_raw` → `band_step_corrected`).
- Disciplina de `ruff` e uma suíte grande.

---

## 11. Plano sugerido

| Prazo | Ação | Itens |
|---|---|---|
| Esta semana, sem GPU | Corrigir roteador/contexto, pedido global, texto ignorado, Esc; emenda Fase 1; completar o trace | Rel. 1: 1, 4, 5, 6, 7, 2-F1; O1 |
| Próximo pod | Validar o acima; experimentos de redesenho, silhueta por SAM, balões, pré-carga, resposta fixa; tempos por etapa da troca | Rel. 1: 3, 2-F2, 9, 11, N1; P4 |
| Antes de lançar a 1.19 | Atualizar documentos e checklist; commitar; refazer o TAR | D1, D3, 13, 14 |
| Depois do lançamento | Tabela de decisão testada e divisão do `_routed`; divisão do arquivo; CI; trava de dependências | A1, A2, Q1, Q2 |
| Quando sobrar tempo | mypy por módulo; testes do JS; limpeza de ramos e `dist/`; segunda rodada de auditoria | Q3, Q4, H1 |

## 12. O que ficou sem revisão (segunda rodada)

1. Leitura de ponta a ponta de `hub_application.py`, `stack_application.py`, `launcher.py`, `registry.py`, `processes.py` (corridas, processos órfãos, identidade de porta).
2. Auditoria de segurança completa (lista em "Não auditado").
3. `scripts/capella-linux.sh` e `posix_runtime.py`.
4. Frontend: acessibilidade, estados de erro, renderizações.
5. Tempos por etapa nas outras três sessões de trace.
6. Referências externas com leitura das fontes.

Se quiser, refaço essa rodada com subagentes quando o limite da conta voltar; cada frente já está descrita e pronta para disparar.

---

## Medição da suíte

Comando: `.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider --durations=40`, uma execução, com as mudanças sem commit presentes.

| Medida | Valor |
|---|---|
| Resultado | **4.189 passaram, 85 pulados, 0 falhas** |
| Tempo | 704 s (11 min 44 s), em série |
| Avisos | 4, todos `Image.getdata` obsoleto no Pillow 14 (`tests/test_edit_lab.py:161`, `tests/test_image_gpu_serialization_and_trace.py:941`) |

Isto também responde ao item 14 do relatório 1: a suíte completa passa com as mudanças sem commit. O teste de painel intermitente não falhou nesta execução, então não consegui identificá-lo.

Os mais lentos:

| Tempo | Teste |
|---|---|
| 10,3 s | `tests/test_api/test_binding.py::test_nothing_the_validator_approves_fails_to_start` |
| 8,9 s | `tests/test_integration.py::test_dry_run_is_deterministic_and_creates_nothing` |
| 5,8 a 8,6 s (5 testes) | `tests/test_pyenv_environment.py` (criam ambientes virtuais de verdade) |
| 4,0 a 6,6 s (8 testes) | `tests/test_delivery_wave_a.py` (TUI real) |
| 4,5 a 4,9 s (5 testes) | `tests/test_integration.py`, `test_custom_install_destination.py` (plano em simulação) |

Observações:

- Os 40 mais lentos somam cerca de 190 s; o resto do tempo está espalhado em milhares de testes rápidos. O ganho grande é rodar em paralelo: `pytest-xdist` não está instalado. Com 8 processos, a suíte cairia para perto de 2 minutos [INFERÊNCIA; exige conferir se os testes que abrem portas e criam ambientes virtuais são isolados].
- 11 minutos por execução desestimula rodar a suíte inteira a cada mudança. Nesta semana rodamos só os testes relacionados antes de cada commit; foi assim que a suíte completa ficou sem rodar com as mudanças de ontem.
- 85 pulados: são os testes do caminho nativo, que precisam de FastAPI e SQLAlchemy (grupo `native-test` do `pyproject.toml`). Ou seja, **o código de `pepito_native.py` não é exercitado na suíte do dia a dia** [INFERÊNCIA pelo comentário do `pyproject.toml`; não listei os pulados um a um].
- Não medi cobertura: `coverage` não está instalado.

## Sessões anteriores (contagem rápida)

| Sessão | Turnos | Roteador (mediana / pior) | Falhas do roteador | Recusas |
|---|---|---|---|---|
| 08/11 21:42 | 12 | 4,3 s / 44,3 s | 0 | 5 |
| 08/11 22:41 | 1 | 40,9 s | 0 | 0 |
| 09/11 22:20 | 10 | 5,2 s / 56,8 s | 0 | 4 |
| 09/11 23:12 (final) | 38 | 6,3 s / 56,8 s | 0 | 7 |

- Em 61 turnos o roteador nunca falhou nem estourou o tempo: as regras de reserva por palavras (cerca de 300 linhas de expressões regulares) não foram usadas nenhuma vez nestas sessões.
- Toda sessão começa com 41 a 57 s de carga fria (achado P3).
- "Não consegui isolar uma região local" aparece 3 vezes nas sessões anteriores: é o mesmo caso do item 4.
- Em 08/11 00:38:53 há um terceiro caso de "roteador respondeu `chat` com plano preenchido" (mensagem "Esta personagem", resposta a uma pergunta nossa): o mesmo defeito do item 1.
