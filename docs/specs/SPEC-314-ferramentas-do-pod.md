# SPEC-314 — Ferramentas do pod e dos labs

> Registro técnico SFW. As referências históricas a `edit_lab.py`, seus testes e fontes experimentais pertencem ao pacote privado e não são comandos ou fixtures disponíveis no público. A implementação pública de métricas e avaliação compartilhadas é `scripts/lab_common.py` / `scripts/lab_review.html`.

| Campo | Valor |
|---|---|
| Onda | 3 |
| Cobre | TOOL-01, TOOL-02 |
| Prioridade / esforço | P2 / P |
| Possui | `scripts/pod_session.py`; o cálculo da emenda no PC em `scripts/edit_lab.py` |

## PRD
1. **TOOL-01:** `pod_session.py up` grava host/porta/id no `pod-state.json` antes do primeiro passo, para a retomada sem argumentos funcionar mesmo quando a primeira execução falha (12/11).
2. **TOOL-02a:** `edit_lab.py` ganha `seams --out <pasta>`: recalcula `band_step_*` no PC (o venv do ComfyUI no pod não tem OpenCV) e grava no `metrics.json` de cada caso, para a página mostrar. (Em 12/11 foi feito por script descartável `%TEMP%\seams.py` com `region_metrics(seam_ready=True)`.)
3. **TOOL-02b:** `pod_session.py collect` não falha quando há arquivos `-wal`/`-shm` locais abertos (12/11: `OSError: Invalid argument ... webui.db-shm` enquanto o banco local estava aberto) — fechar/ignorar com mensagem clara.
4. Testes unitários.

## Notas da implementação

- `up` persiste destino e caminho absoluto do tar antes da validação/upload. Após falha no primeiro passo, `up --out <pasta>` reutiliza ambos, inclusive tar fora de `dist/`.
- `collect` ignora apenas erros de extração dos arquivos `webui.db-wal`/`webui.db-shm`; avisa para fechar o banco local e repetir a coleta. O manifesto registra `skipped` com o erro, sem calcular hash do arquivo local antigo como se tivesse sido coletado. Outros erros continuam visíveis.
- No PC, execute `swap-venv/Scripts/python.exe scripts/edit_lab.py seams --out <lab>/results`. Usa somente `source.png`, `<variante>_raw.png`, `<variante>_area.png` e o plano salvo em `metrics.json`; não chama serviços nem recalcula SAM. Atualiza métricas e o composto final, preservando prompt, tempo e resultados de outras variantes. Falta de OpenCV/NumPy, entradas ausentes ou erro de emenda são reportados sem salvar aquela medição como sucesso.
- Para incorporar as métricas numa página já gerada, execute o comando existente `review --cases <lab>/cases.json --out <lab>/results`.
- Prova local (12/11/2026): cópia integral em `%TEMP%/pepito-spec314-ssrk5cf1/lab-regiao`, sem visualizar imagens. O comando `seams` no swap-venv reprocessou 28 variantes de 7 casos; os 224 valores de referência em `lab-regiao/seams.json` coincidiram exatamente (zero diferenças), incluindo tempos preservados. O original não foi alterado.
- Validação: Ruff format/check nos quatro arquivos Python alterados; 80 testes dos labs e da sessão do pod passaram. Os dois avisos são de uso preexistente de `Image.getdata`.
- No pod: validar SSH/coleta reais e repetir uma coleta com o banco local aberto; a reprodução unitária cobre `OSError(22)` em ambos os sidecars. Não foi necessária geração por GPU.
