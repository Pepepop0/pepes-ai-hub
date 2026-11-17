# LAB-04: execução de 15/11/2026

> Registro técnico SFW. As referências históricas a `edit_lab.py`, seus testes e fontes experimentais pertencem ao pacote privado e não são comandos ou fixtures disponíveis no público. A implementação pública de métricas e avaliação compartilhadas é `scripts/lab_common.py` / `scripts/lab_review.html`.

- Pod `<id>` (RTX 5090, 32 GB). Filtro 0.27.0, Hub 1.19.0. Só o lab: sem interface, conta nem chat.
- **136 de 136 registros ok**, nenhuma falha: 12 combinações (R1 a R8; R1 a R4 em Qwen e Krea, R5 a R8 só em Qwen), 2 sementes, variantes V0 a V5. 36 chamadas de escrita (9 no piloto, 27 no restante), com raciocínio e resposta de cada uma gravados.
- Tempos por imagem: Qwen 8,4 s em média (máx. 16 s); Krea 14,8 s (máx. 25 s); piloto de 34 imagens mediana 10,5 s. Muito abaixo da estimativa de 20 a 90 s.
- Cópia para o PC: `Downloads/pod-onda-5/lab-referencias` (900 arquivos, 728.728.023 bytes no pod; 688 arquivos de resultado conferidos por SHA-256, 0 divergências). Avaliador único em `results/avaliar-prompt.html` (24 grupos, 112 comparações a avaliar).

## Incidentes (para não repetir)
1. **A instalação parou sozinha** às 16:48 UTC, no meio do download do Ollama, porque o processo local que a comandava morreu; ficou ~18 min sem andar, despercebido. Retomada com o mesmo comando (`up`). Já anotado como TOOL-03 (visão do progresso com aviso de parada).
2. **O piloto falhou antes de gerar imagens** (`Uma edição local não pode esticar a imagem`): o lab congelava o canvas em 1024×1024, e 5 das 7 cenas não são quadradas. O ensaio sem GPU não decodifica as imagens e não pegou. Correção: o canvas mantém o orçamento de ~1 MP e segue a proporção da fonte principal, como o filtro. Teste de regressão em `tests/test_edit_lab_prompt.py`. A decisão da SPEC-320 de "canvas 1024×1024" foi trocada por "~1 MP na proporção da fonte principal".
3. Canvas resultantes: R1/R2 1120×992 (Qwen) e 1088×976 (Krea); R3/R4 832×1216 (Qwen); R5 832×1248; R6 1056×928; R7/R8 1024×1024.

Avaliação do usuário e análise: pendentes.
