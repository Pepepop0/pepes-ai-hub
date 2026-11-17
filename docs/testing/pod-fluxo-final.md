# Pod — teste final de fluxo

Escopo: teste no Open WebUI real, com conta do usuário e GPU. **Sem novos labs.** O filtro deste pacote é o **0.29.0** (correções da análise do pod de 15/11, em `docs/ARCHITECTURE.md`). O nome dos artefatos ainda usa Hub **1.19.0**, como nos arquivos de versão; a renumeração proposta em `docs/VERSIONING.md` não foi aplicada. Não confundir o contador interno do filtro com a versão do Hub.

## Comportamento do pacote

- Uma fonte: mantém o par E/C já implementado.
- Duas ou três referências compatíveis com o workflow: **V2 primeiro** (visão + contrato de referências) e **V3/E depois** (revisão sem raciocínio + contrato).
- Ambos recebem as mesmas fontes na mesma ordem e geram com a mesma semente. Os prompts finais e os escritores ficam na receita de cada saída.
- “Prefiro esta” escolhe a base das próximas edições, mantém a alternativa no histórico e persiste ao recarregar.
- Saída inteira por padrão: `area_paste=False`, sem recortar/colar a área. Deformação mantém o compositor próprio.
- Troca de personagem: variação 1 **Qwen inteira** (caminho do LAB-04, sem colagem), variação 2 **Qwen com marcação** composta.
- Edições de uma fonte voltam ao tom da fonte nas partes que o pedido não muda (sem escurecer a cada edição), exceto quando o pedido fala de luz ou cor.
- Geração nova, troca de personagem, deformação, regeração de receita e pedido explícito de várias imagens não disparam um par adicional.
- O par multirreferência é uma escolha para validar em fluxo, não uma afirmação de vencedor universal do LAB-04. R3 e R4–R7 daquele lab foram excluídos das conclusões pelo usuário.

## Preparação

1. Usuário cria pod RTX 5090 32 GB, mesmo template PyTorch/CUDA, com pelo menos 150 GB livres, e fornece SSH/porta/id.
2. Assistente verifica hardware/espaço, envia o TAR e SHA-256 da entrega e instala usando `scripts/pod_session.py up`, com log numa pasta nova (não reutilizar estado de outro pod).
3. Acompanhar o log com `scripts/pod_watch.py`. Ausência de eventos é aviso para investigar, não prova de travamento nem autorização para iniciar um segundo instalador simultâneo.
4. Abrir túnel para o Open WebUI e avisar explicitamente **Sua vez**. Usuário cria a conta; assistente executa `start` novamente para sincronizar os presets e filtros. Ctrl+F5.
5. Verificar filtro 0.29.0 e Valves `prompt_pair=True`, `area_paste=False`. Não rodar lab nem outro trabalho de GPU junto com o teste.

## Testes do usuário, em ordem

Usar primeiro imagens simples e não explícitas; identificar claramente o que vem de cada fonte. Em caso de falha, parar naquele bloco e guardar pedido, horário e descrição. Não repetir tudo indiscriminadamente.

| Bloco | Pedido/ação | Resultado esperado |
|---|---|---|
| 1. Imagem nova | Gerar uma pessoa adulta de casaco vermelho numa praça | Uma imagem, sem duplicação involuntária; abrir/zoom/arrastar/download funcionando |
| 2. Uma fonte | “Deixe o casaco azul, mantendo o resto” | Duas versões E/C lado a lado; nenhuma caixa colada |
| 3. Preferência | Escolher a segunda com “Prefiro esta”; recarregar; “Agora coloque um chapéu nela” | Escolhida primeiro com selo, alternativa nas setas; nova edição usa a escolhida |
| 4. Duas referências Qwen | Anexar uma cena e uma pessoa distinta; “Use a personagem da imagem 2 na cena da imagem 1, mantendo a pose e a luz da cena” | Duas versões: V2 e E; fontes corretas, sem inversão de papéis; não basta colar a folha da personagem sobre a cena |
| 5. Continuação do par | Escolher uma das versões do bloco 4 e pedir “Agora deixe o casaco verde” | A imagem escolhida passa a ser o canvas; a referência antiga não toma seu lugar indevidamente |
| 6. Troca com duas fontes | Cena e personagem de fontes não explícitas; pedido de troca de personagem | Duas versões na ordem 1 Qwen inteira e 2 Qwen com marcação; respeita limite de duas fontes e mantém escolha/receitas |
| 7. Três referências Qwen | Cena da 1, pessoa da 2, uma luz claramente diferente da 3; identificar os três papéis no pedido | Todas as fontes usadas nos papéis certos; duas versões. Nenhuma fonte descartada em silêncio |
| 8. Área pintada | Editar uma área com um pedido simples de cor | Padrão entrega a geração inteira, sem retângulo colado; efeitos fora do alvo contam como falha de qualidade, não devem ser escondidos |
| 9. Receita | Numa versão escolhida: Prompt → regerar mesma semente; depois editar o texto e regerar | Uma saída por ação; prompt/fontes/semente correspondem à versão escolhida; não cria outro par nem duplica prefixos |
| 10. Colagem opcional | Ligar “Colar só a área pintada”, fazer uma edição com área; abrir “Ver sem colagem” e “Refazer na imagem inteira” | Bruta visível sem mensagem nova; refazer entrega imagem inteira. Desligar a opção depois |
| 11. Regressões | Troca de personagem; pedir três imagens; Esc durante resposta | Troca entrega Qwen inteira e Qwen com marcação; pedido de três entrega três, não seis; Esc para resposta sem apagar botões |
| 12. Correções de 15/11 | Mesmas fontes da troca loira ↔ cabelo preto; depois "tira essa capa"; uma edição de forma corporal (a body-shape edit); quatro edições seguidas de cor numa mesma imagem | Variação 1 sem aspecto colado, comparável ao R1/R2 do LAB-04; capa sai sem levar os braços; a forma pedida muda (Qwen); brilho não cai a cada edição |

## Conferência pelo assistente

- Em multirreferência, evento `prompt_pair` com `writers=["vision","revise"]`; monoimagem continua `["revise","vision_think"]`. Na troca, `swap_whole_prompt` com `writer="vision"`; nas edições, `tone_match` com `applied`/`reason`.
- Receitas dos pares: mesma semente e lista ordenada de fontes, prompts próprios com envelope uma única vez; regeneração mantém o prompt gravado.
- Preferência persistida no grupo e seleção usada no turno seguinte.
- Logs Open WebUI/ComfyUI: nenhuma exceção bloqueadora ou falha silenciosa. Medir tempos reais, sem prometer prazo com base só em fakes.
- Não visualizar imagens sensíveis: usar trace/banco/logs e os relatos do usuário.

## Encerramento e lançamento

Trazer trace, banco, entradas/saídas e logs com manifesto SHA-256; conferir no PC; só então tocar som e informar **PODE DELETAR O POD**. Exclusão do pod cabe ao usuário. Falha bloqueadora exige correção e repetição do cenário afetado. Aprovação do fluxo não cria tag, push ou release automaticamente: lançamento depende de autorização explícita do dono.

## Verificação local desta entrega

- 300 testes focados passaram no ambiente nativo: pares mono/multirreferência, receitas, regeração, salvaguardas e seleção/preferência.
- Suíte completa: 4.738 passaram, 123 pulados, 1 falha no teste legado TUI `test_profile_selection_updates_compatibility_plan_and_right_rail`: após clicar em Capella, a tela ainda mostrava Pepe. Causa não confirmada; não afirmar suíte verde nem lançamento pronto. Nenhum código desse painel foi alterado nesta entrega.
- O teste real do Open WebUI no pod continua necessário para aprovação funcional e de qualidade das imagens.
