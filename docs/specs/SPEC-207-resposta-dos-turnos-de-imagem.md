# SPEC-207 — Resposta dos turnos de imagem sem recarregar o modelo

| Campo | Valor |
|---|---|
| Onda | 2, fases A, B e C |
| Cobre | PERF-01 (N1), DEC-07 |
| Prioridade / esforço | P1 / P (1 dia + 10 min de pod) |
| Depende de | SPEC-110 (pré-carga), integrada |
| Desbloqueia | SPEC-210 |
| Possui | O fim do gancho `request` (depois de liberar o ComfyUI) e o `outlet` para turnos de imagem; Valve `image_reply` |
| Pod | Sim |
| Status | **descartada (10/11)** |

## PRD

**Problema.** Em todo turno de imagem, depois do job, o modelo de chat de 20 GB é recarregado (6,0 a 6,3 s) só para escrever uma frase de 15 a 21 tokens. Num turno típico de 27 s isso é cerca de um quarto.

**Objetivo.** A imagem chega e o turno termina sem esperar a recarga, sem perder a resposta em português que o usuário espera.

**Critérios de aceite.**

1. Valve `image_reply`: `model` (hoje) ou `fixed`. Com `fixed`, a resposta é montada pelo filtro: "Pronto." / "Pronto: 2 variações." / a nota de referências usadas / o aviso de nova tentativa, em português, sem chamar o modelo.
2. Com `fixed`, o gancho não espera `wait_comfyui_release` antes de responder; a recarga do modelo acontece quando o próximo turno precisar (o roteador já trata modelo ausente).
3. O trace registra `reply_mode` e `turn_end.total_seconds`.
4. No pod: 10 turnos de edição em cada modo; padrão = `fixed` se o tempo médio por turno cair ≥ 5 s **e** você não sentir falta do texto do modelo.
5. Respostas de erro, recusas e "Anotado." não mudam.

## Spec técnica

- Com `fixed`, o filtro devolve a resposta pelo mesmo caminho das respostas fixas (`fixed_reply`), com as imagens anexadas como hoje.
- `free_comfyui` continua sendo chamado, sem bloquear a resposta.
- Atenção à SPEC-105: a nota de "nenhuma imagem" não se aplica a turnos com imagem.

## Testes

Unitários: modo `fixed` não chama o Ollama no turno de imagem; texto para 1 e 2 variações e com referências; modo `model` igual a hoje.

## Tarefas

1. Valve e montagem do texto.
2. Não bloquear na liberação no modo `fixed`.
3. Medição no pod; fixar o padrão (fase C).

## Riscos

O próximo turno de conversa paga a recarga (6 s) que antes vinha no fim do turno de imagem. A troca é boa quando os turnos de imagem são seguidos, que é o uso observado.

## Por que foi descartada (10/11)

A fase A foi implementada (ramo `spec/207-resposta-fixa`, `26768b9`) e não integrada. O Open WebUI 0.12.0 sempre envia o turno ao modelo de chat depois do filtro; uma resposta fixa só troca o texto que o modelo recebe (`fixed_reply` → `REPLY_CONTEXT`). O trace de 09/11 confirma: as respostas fixas de recusa rodaram o modelo (18 a 60 tokens). Num turno de imagem o modelo foi descarregado para a geração, então seria recarregado do mesmo jeito (cerca de 6 s): o ganho some. E, sem a espera pela liberação do ComfyUI, essa recarga poderia acontecer com a placa ainda ocupada.

O caminho que resolve é responder (e rotear) com um modelo pequeno que fica residente junto do modelo de imagem: PERF-02, numa spec depois do pod.
