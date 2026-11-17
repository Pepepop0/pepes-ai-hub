# SPEC-309 — Quem escreve o prompt: roteador, dois passos com visão, raciocínio, palavras cruas

> Registro técnico SFW. As referências históricas a `edit_lab.py`, seus testes e fontes experimentais pertencem ao pacote privado e não são comandos ou fixtures disponíveis no público. A implementação pública de métricas e avaliação compartilhadas é `scripts/lab_common.py` / `scripts/lab_review.html`.

| Campo | Valor |
|---|---|
| Onda | 3 (A local; B lab no pod; C decisão) |
| Cobre | Pedido do usuário (12/11, ponto 2) |
| Prioridade / esforço | P1 / M |
| Depende de | SPEC-315 (avaliador) para o lab |
| Possui | Valve nova `prompt_writer` e o passo de escrita do prompt depois de `route()` em `pepito_image_chat.py`; um modo novo `prompt-lab` em `scripts/edit_lab.py` |
| Pod | Sim (lab) |

## PRD
**Problema.** O roteador é o mesmo Qwen 27B do chat, mas escreve o prompt às cegas (não recebe a imagem, só uma descrição de 100 caracteres), num JSON de ~20 campos, sem raciocínio, com histórico curto, e nosso código cola listas fixas ("Requested changes / Keep unchanged / Source purpose") que às vezes se contradizem.

**Variantes** (Valve `prompt_writer`, padrão `router` = hoje):
- **A `router`**: hoje.
- **B `vision`**: o roteador decide (ação, modo, fontes); um segundo passo do mesmo modelo, **com a imagem de origem anexada**, escreve só o prompt final (texto livre, temperatura moderada), sem as listas coladas.
- **C `vision_think`**: uma chamada do roteador com a imagem anexada e raciocínio ligado (conferir se o Ollama aceita `think` com `format`; se não, registrar e implementar C como B com raciocínio).
- **D `raw`**: as palavras do usuário (português) direto ao modelo de imagem, sem prompt escrito — controle.

**Critérios de aceite (fase A).**
1. As quatro variantes existem atrás da Valve; padrão inalterado.
2. Trace `prompt_writer`, `prompt_seconds`, e o prompt final em `image_job.params.prompt`.
3. A salvaguarda de idade roda sobre o prompt final em todas as variantes.
4. Lab `edit_lab.py prompt-run`: para cada caso real (pedido + origem + parte + área + semente do trace), gera as 4 variantes × 2 sementes, mesma origem/parte/área/semente; A usa o prompt **gravado no trace** daquela sessão. Saída para o avaliador único (SPEC-315) com A como base e o prompt embaixo de cada imagem.
5. Casos sugeridos (~12): A1–A5 e B1–B2 do lab de área, "Você só fez uma parte, ainda faltou tirar essas peças" (12/11 23:21:36), "Da cintura pra cima" (23:43:40), "Agora faz ela em um quarto sozinha" (23:34:17), "Faça uma versão fotorrealista dessa personagem" (23:40:06), "Uma edição de forma corporal (a body-shape edit) nas coxas e no quadril" (23:29:25).

**Regra de decisão (fase C):** a variante com mais `+`/`++` contra a base e nenhum `--`, desde que o tempo extra por turno fique ≤ 5 s; empate → a mais rápida.

## Notas da implementação — fase A

- `prompt_writer` é uma UserValve com `router` por padrão. B escreve texto livre (temperatura 0,5), depois da escolha/validação das origens e antes de descarregar o Ollama. C faz o roteamento com as imagens do catálogo anexadas e `think: true` + o schema `format`; se o Ollama rejeitar explicitamente essa combinação (HTTP 400), registra `prompt_think_compatibility` e usa roteamento normal seguido de B com raciocínio. Não se presume compatibilidade sem chamar o pod. Erro do escritor não vira silenciosamente outra variante.
- B/C/D não recebem as listas automáticas de mudanças/preservação/finalidade. D conserva o português da mensagem já sem os trechos ignorados. O evento `prompt_writer` registra variante e `prompt_seconds`; o prompt efetivo continua em `image_job.params.prompt`. A salvaguarda também verifica a saída do escritor e o prompt final montado.
- O modo do lab chama-se `prompt-prepare`/`prompt-run` (não `prompt-lab`). O preparo lê os traces, a ramificação do chat no DB quando disponível e a numeração persistida de imagens; copia os uploads sem alterar os dados históricos. No arquivo antigo sem DB, o histórico textual vem dos eventos reais do trace.
- Conjunto: A1–A5 e B1–B2, mais P1–P5 na ordem dos cinco horários acima. P1 recupera a máscara pintada original de `pepito-characters/*.pictures.json`, `last_edit.mask_png`, conferindo o prompt. Não redesenha nem aproxima a máscara. Os demais casos locais usam as caixas/alvos e a dilatação registrados. A máscara preparada é única por caso e usada nas quatro variantes.
- A base usa **literalmente** `image_job.params.prompt`, não um prompt refeito pelo código atual. Parte/ferramenta, origem, área, ref_boost e canvas ficam congelados no manifesto; a resposta de C só fornece o prompt, nunca troca esses parâmetros.
- Sementes: a primeira é a do trace, ou a recuperada do metadado textual do PNG ComfyUI, se houver. Na ausência de ambas, duas sementes fixas do experimento (`309000 + índice × 10`, e +1), com `seed_origin: experiment`; nunca se atribui ao trace uma semente que ele não gravou. A segunda semente (+1) é sempre do experimento. O objetivo é comparar os prompts sob os mesmos parâmetros, não reproduzir os pixels históricos.
- `prompt-run` prepara B/C no Ollama do pod (modelo de chat do trace ou `--vision-model`) antes de descarregá-lo. Depois executa 4 × 2 jobs por caso, agrupados por ferramenta. A composição usa `region_metrics`, compartilhado com o lab de área: a mesma máscara em todas as variantes, composição simples quando o ComfyUI não tem OpenCV (registrado em `seam` nas métricas), e o compositor de emenda do filtro quando as dependências estão presentes. Não exige instalar nada no pod. `prompt_seconds` é separado do tempo do job.
- `--dry-run` não acessa serviços: grava as requisições Ollama completas com a origem em base64, os grafos A/D e os oito registros por caso. B/C ficam com prompt `null` e sem grafo até existir resposta real; não há prompt inventado nem imagem simulada. `avaliar-prompt.html` é produzido por `review_page`, com base A na mesma semente, prompt por variante e rodada própria em `review-rounds.json`.

### Preparar e executar

```text
python scripts/edit_lab.py prompt-prepare --session <pod-onda-2> --session <sessao-final-20261109-2312> --region-cases <lab-regiao/cases.json> --out <lab-prompt>
python scripts/edit_lab.py prompt-run --cases <lab-prompt/cases.json> --out <lab-prompt/results> --dry-run
python scripts/edit_lab.py prompt-run --cases <lab-prompt/cases.json> --out <lab-prompt/results> --ollama http://127.0.0.1:11434 --comfyui http://127.0.0.1:8188
```

O lab local fica em `$HOME/Downloads/pod-onda-3/lab-prompt/`. Levar `cases.json`, `sources/` e o código atualizado ao pod; executar a última linha no ambiente com as dependências e avaliar os resultados reais. Só então aplicar a regra da fase C (qualidade relativa e custo adicional ≤ 5 s). Os testes afetados são `tests/test_image_prompt_writer.py` e `tests/test_edit_lab_prompt.py`, além das regressões de filtro/lab.

**Preparo real de 12/11.** Os 12 casos sugeridos foram mantidos, sem substituições. A máscara de P1 foi recuperada da receita persistida; a semente ausente no trace de B2 foi recuperada do PNG do ComfyUI (`seed_origin: comfy_png`, primeira semente `359047918257410`). O dry-run local produziu 96 registros `dry_run`, 24 requisições Ollama completas e 48 grafos A/D, sem falhas e sem chamar serviços. Os helpers do filtro são carregados sem a classe de hooks/Valves nem o import Pydantic, preservando as funções de produção e permitindo o lab no ambiente ComfyUI/Pillow sem instalar dependências do Hub.

**Salvaguarda sobre o prompt montado.** A verificação final expôs um falso positivo da regex de idade: `age 1` era reconhecido dentro de `Image 1`, bloqueando referências e trocas. Com autorização da integração, acrescentou-se a fronteira de palavra antes de `idade|age|aged|aos`. Os testes cobrem `Image 1`/`image 15` como identificadores e mantêm o bloqueio de `age 15`, `age: 15`, `aged 15`, formas em português e idades por extenso, além de toda a suíte de menores já existente.




### Conclusão da integração (13/11)

O agente parou por limite de uso sem commit; o integrador preservou o trabalho, concluiu lint e regressões e integrou o código. A nova checagem do prompt final encontrou um falso positivo preexistente: o padrão de idade capturava `age 1` dentro de `Image 1`. Foi acrescentada a fronteira de palavra em `_AGE_DIGITS`, com testes de identificadores de referência e idades reais. A salvaguarda permanece obrigatória. Nenhuma chamada de modelo foi feita na preparação local.
