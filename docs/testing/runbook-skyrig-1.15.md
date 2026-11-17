# SkyRig — runbook econômico do Hub 1.15.0

Instale **só a 1.15.0**: ela contém as versões 1.12, 1.13 e 1.14. As versões separadas servem para rastrear mudanças, não precisam ser testadas uma por uma.

Não refaça a escada inteira nem a conversa longa agora. Primeiro valide estes caminhos curtos, com imagens de adultos não explícitas. Se falhar, anote o horário, não insista em cinco novas gerações e envie o histórico.

## Instalar sem baixar modelos novamente

1. Feche o Hub anterior. Extraia o ZIP novo numa pasta separada e abra o executável novo.
2. Em Instalações, selecione a Capella existente e use **Reparar**, no mesmo destino. Não desinstale nada.
3. Inicie os serviços e recarregue o Open WebUI (Ctrl+F5).
4. Confira Hub **1.15.0** e filtro Pepito Image Chat **0.17.0**. Abra um chat novo em Capella.
5. Menu Integrations: **Image ligado**. Comece com Buscar referências desligado.
6. Ícone Controls no alto à direita → **Valves** → categoria **Functions** → **Pepito Image Chat**. `ref_boost fixo = 0`, Fidelidade das edições automática e Usar a calibragem aprendida ligado; salve se editar algum campo.

## Sem gerar nada: UX

Na primeira imagem da sessão:
- Só a imagem deve aparecer, com cantos levemente arredondados. Sem o prompt encaminhado ou painel de base.
- Hover/foco mostra **Editar** e **⋯**. No toque as ações continuam disponíveis.
- Clique na imagem abre o viewer de tela cheia. Teste zoom, Ajustar, navegação, Download e Esc para voltar.
- **Editar** continua abrindo o canvas grande com pincel, borracha e desfazer.
- Anexar uma área cria um chip editável/removível. Nenhum código entra no texto.
- Com área anexada e Image desligado, tente enviar: o texto e a área devem permanecer para correção. Depois ligue Image ou remova a área.

## Cinco pedidos curtos

| # | Pedido | Critério de aprovação |
|---|---|---|
| 1 | `faz uma foto de uma mulher adulta de casaco vermelho num café` | Uma imagem, não um par oculto. |
| 2 | `deixa o casaco azul, mantendo o rosto e o fundo` | Região automática quando o modelo visual a localizar com segurança; se houver dúvida, pede pintura manual. Fora da máscara os pixels e a resolução originais ficam preservados. |
| 3 | `agora faça só um sorriso, sem mudar a roupa` | O pedido novo chega ao modelo; não repete a instrução do casaco. |
| 4 | `repita exatamente a última edição` | Repete a receita completa, com a mesma quantidade e fontes. Não vira outra operação nem cresce o número de tentativas. |
| 5 | Remova qualquer área anexada e peça `agora reenquadre em 16:9 horizontal` | Canvas realmente horizontal. Uma pose incompatível ou conflito com máscara pede esclarecimento, não gera no formato errado. |

Para validar preservação, compare a origem e o resultado em tamanho cheio. Uma região automática é uma estimativa visual com margens, não segmentação perfeita de cada fio de cabelo. Se a região não corresponder ao que quer, pinte manualmente.

## Calibragem sem novas gerações

- **⋯ → Usar esta** muda apenas a origem da próxima edição. Não é aprovação e não ensina sucesso.
- **⋯ → Aprovar resultado** registra aceitação. Também pode digitar `aprovo a imagem N` usando o número da imagem.
- Aprovar a mesma imagem novamente não ensina duas vezes.
- Só resultados automáticos/corrigidos aprovados treinam a fidelidade. Resultados manuais, falhas e cancelamentos não treinam.
- Uma escolha explícita é consumida quando a próxima leva é entregue; o próximo refinamento avança para o novo resultado em vez de ficar preso na origem antiga.
- O reset fica em **⋯ → Preferências do editor → Zerar calibragem aprendida**. É por conta e pede confirmação; use só se quiser realmente apagar seus ajustes.

## Comparação opcional de editores

Não precisa fazer isto se o fluxo acima falhar. Para um A/B curto:
1. Escolha explicitamente a mesma imagem original para cada execução.
2. Em **⋯ → Preferências do editor**, selecione o editor instalado desejado e a mesma seed de comparação (por exemplo 42).
3. Use o mesmo pedido, área e quantidade. Faça uma execução por editor, sem conversa longa.
4. Compare resultado e tempo. Mesmo seed não significa ruído ou resultado idêntico entre arquiteturas.
5. Volte o editor para o roteamento do chat e limpe a seed quando terminar.

O sistema não baixa nem troca modelos silenciosamente. Se um editor não estiver instalado, pede outra escolha. O editor mantém seus limites de papéis e referências; mais fontes do que o workflow aceita exigem esclarecimento.

## Encerrar

Rode `enviar-historico-skyrig.bat`, espere terminar e mande os números dos testes que passaram/falharam e os horários. Depois pode desligar. Não precisamos repetir a bateria longa para confirmar defeitos que já foram reproduzidos em CPU.

## Limite desta entrega

A integração, ownership, composição de pixels, ledger, controles e fluxos sem modelos foram verificados localmente. A precisão da localização visual, adesão do editor e desempenho no RTX 5090 dependem da inferência nessa máquina. Nenhuma medição de pixels é usada como prova de cumprimento semântico.
