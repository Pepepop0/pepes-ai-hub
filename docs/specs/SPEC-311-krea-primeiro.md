# SPEC-311 — Troca: mostrar a variação do Krea primeiro

| Campo | Valor |
|---|---|
| Onda | 3 |
| Cobre | UI-05 (pedido U3) |
| Prioridade / esforço | P1 / P |
| Possui | A ordem das imagens entregues e a nota "Variação 1/2" em `swap_chat_image` (`pepito_image_chat.py`, ~7038–7076) |

## PRD
Na troca, a imagem do Krea (hoje "Variação 2") aparece primeiro. Só a ordem de exibição muda: as duas continuam geradas, e a nota passa a ser "Variação 1: Krea. Variação 2: Qwen com marcação." "usa a 1" passa a escolher o Krea. Testes da troca atualizados para a ordem nova.
