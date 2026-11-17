# Laboratório A/B/C/D — Onda 3

## Escopo corrigido pelo usuário

Este pod foi usado **somente para o laboratório de prompts**. A instalação da UI/stack completa foi uma interpretação indevida do assistente e acrescentou preparação desnecessária. Não houve criação de conta nem validação manual da aplicação. Nenhum teste de UI pode ser dado como aprovado por este laboratório.

## Pod e preparação

- Lium, Almaty, RTX 5090 (32607 MiB), driver 595.91.07, 109 GiB de RAM, 14 CPUs. Preço exibido: US$ 0,75/h.
- SSH fornecido pelo usuário: `<usuario>@<host> -p <porta>`, HostKeyAlias `lium-pod-<id>`.
- Instalação da stack retomada após a primeira execução parar durante um download. A retomada completou em cerca de 24 minutos. Não se atribuiu a interrupção a uma causa de rede sem prova.
- Filtro/código do lab embarcados: 0.23 / Hub 1.19.0.
- Túnel da UI encerrado após o usuário esclarecer o escopo. Ollama e ComfyUI usados pelo lab.

## Desenho do experimento

12 pedidos reais × 4 variantes × 2 sementes = **96 jobs**. Fontes, parte/modelo, área, canvas, intensidade e sementes congelados por caso; o texto do prompt é o fator variado.

| Variante | Texto para o gerador |
|---|---|
| A `router` | Prompt final literalmente gravado na sessão histórica |
| B `vision` | Segundo passo do modelo de chat vendo a origem |
| C `vision_think` | Roteador com a imagem e raciocínio; só seu prompt é usado no lab |
| D `raw` | Palavras do usuário em português, sem tradução e sem notas ignoradas |

A máscara pintada e a semente ausentes do trace foram recuperadas da receita e do PNG ComfyUI, sem inventar dados nem substituir casos. O ensaio no Python do pod passou antes da geração real.

## Resultado operacional verificado

- Processo terminou com `run.exit=0`, em **1034,9 s** (~17 min 15 s).
- **96 registros `ok`: 24 por variante**, zero falhas registradas.
- Cópia local: **347 arquivos, 297.763.211 bytes** de saídas. Todos conferidos contra o manifesto remoto por tamanho e SHA-256.
- Preservados também: manifesto de casos, fontes/máscara, grafos, prompts, métricas, `run.log`, `run.exit` e logs de Ollama/ComfyUI/Hub.
- Não foram abertas imagens das sessões/lab pelo assistente; qualidade depende da avaliação do usuário.

## Custo de escrever os prompts

Valores por caso, sem contar duas vezes a mesma escrita usada pelas duas sementes:

| Variante | Média (s) | Mediana (s) | Máximo (s) |
|---|---|---|---|
| B visão | 4,253 | 3,018 | 17,655 |
| C visão+raciocínio | 11,352 | 10,777 | 15,465 |

**Limite da comparação de tempo:** A reaproveita um prompt histórico e D usa o texto pronto; os zeros delas não significam roteamento gratuito em produção. B mede o segundo passo adicional. C mede sua chamada de roteamento com visão/raciocínio. Não interpretar C como 11,352 s adicionais a outro roteamento nem comparar estes números como tempos completos de turno. Ainda faltam notas humanas e, para uma decisão final de latência, considerar o roteamento comum às variantes.

## Onde avaliar

`$HOME/Downloads/pod-onda-3/lab-prompt/results/avaliar-prompt.html`

- 24 comparações (12 pedidos, duas sementes).
- A é a base à esquerda; B/C/D são avaliadas contra A, de `--` a `++`.
- Prompts podem ser expandidos; comentários, favoritas, marcas e culpa do prompt ficam na exportação.
- Notas isoladas por laboratório/rodada. O usuário não precisa manter o pod ligado para avaliar.
- Falta: avaliação/exportação do usuário. **Nenhum escritor novo foi escolhido como padrão.**

## Encerramento

Som tocado e mensagem explícita **PODE DELETAR O POD** enviada após a conferência das cópias. Página local aberta via navegador padrão. Exclusão no provedor cabe ao usuário e não foi verificada.

Registro operacional completo: `Downloads/pod-onda-3/SESSION.md`. Manifesto de saídas: `Downloads/pod-onda-3/lab-prompt/manifest-results.json`.

## Avaliação do usuário (14/11)

Exportação: `Downloads/avaliacao-prompt-20261114-024907-c4c6.md`. Completa: 24 comparações × 3 candidatas = 72 notas, 88 marcas pintadas, nenhuma "culpa do prompt". Escala contra A (roteador), `--` = −2 … `++` = +2:

| Escritor | `--` | `-` | `=` | `+` | `++` | Média | Tempo do prompt (média) |
|---|---|---|---|---|---|---|---|
| B visão | 2 | 5 | 7 | 6 | 4 | +0,21 | 4,3 s |
| C visão + raciocínio | 0 | 3 | 6 | 3 | 12 | **+1,00** | 11,4 s (máx. 15,5) |
| D palavras cruas | 8 | 4 | 6 | 4 | 2 | −0,50 | 0 |

- C venceu com folga e nunca foi muito pior que A. Falha recorrente: A1 escureceu a pele quando o pedido era pálida.
- D é descartado como padrão. Em A4 apareceu uma criança na cena: o texto cru não carrega o enquadramento adulto que o prompt escrito traz.
- Marcas de corte em A1, A2 e P1 aparecem em todas as variantes, A inclusive: vêm da área em caixa gravada na sessão (IMG-13, corte estrutural), não do prompt.
- P2 e P4 ("mulher cortada"): não houve falha de execução. O lab congelou o canvas da sessão de 12/11 para isolar o prompt: P2 560×848 (retrato, 0,47 MP) e P4 736×640 (0,47 MP), vindos de uma fonte 736×645. Com o filtro 0.23 (SPEC-304, mínimo de 1 MP), o mesmo pedido sairia 832×1248 e 1088×960. As notas de P2/P4 comparam prompts com o canvas antigo; servem como comparação relativa, não como qualidade absoluta.
- Limites: 12 pedidos, 2 sementes, um avaliador.
