# Backlog da madrugada — implementação concluída

Entrega cumulativa: Hub **1.15.0**, com etapas separadas em 1.12.0, 1.13.0 e 1.14.0.

## Execução e conversa longa — 1.12

- [x] Pedido novo/refinamento separado de repetição pura; uma reclamação não apaga um delta novo.
- [x] Receita completa de retry, sem forçar edição local nem apagar pose/fontes.
- [x] Base governa o slot efetivo; papéis incompatíveis pedem esclarecimento.
- [x] Formato solicitado chega ao canvas, respeitando limites do workflow.
- [x] Confirmação factual por arquivos entregues; sem afirmações de erro/Image off depois de sucesso.
- [x] Quantidade solicitada separada de tentativas; sem crescimento por retries incidentais.

## Calibragem — 1.13

- [x] Seleção, publicação e aprovação distintas; aprovação digitada e de interface usam o mesmo helper.
- [x] Ledger SQLite transacional, idempotente, por conta/família/workflow/operação.
- [x] Só resultado automático/corrigido entregue e aprovado ensina; manual, falha e cancelamento não.
- [x] Rejeição não prova que a tentativa seguinte funcionou.
- [x] Limites permitem aprendizado abaixo do antigo piso de 1,5; esquecimento gradual e reset por conta.
- [x] Migração única do legado, sem transformar offsets de rejeições em sucessos confirmados.
- [x] Sem retries cegos por MAD global; avaliação de preservação é telemetria localizada.

## Degradação e editores — 1.14

- [x] Composição final em pixels, mantendo RGBA e resolução originais fora da máscara.
- [x] Localização visual automática conservadora com o modelo já configurado; ambiguidade pede pintura.
- [x] Máscara manual prioritária; operações globais/geometry não recebem máscara local arbitrária.
- [x] Canvas global limitado com aviso; HQ não esconde gerações extras descartadas.
- [x] Preparação de referências Qwen para canvas/reenquadramento sem esticar a origem.
- [x] Escolha explícita de editor instalado e seed de comparação; nenhuma troca/download silencioso.

## UI/UX pedida nos prints — 1.15

- [x] Imagem clean, cantos arredondados; sem prompt encaminhado, cabeçalho, base e dicas permanentes.
- [x] Editar e ⋯ no hover/foco; ações utilizáveis por toque.
- [x] Clique amplia em viewer de tela cheia; zoom/fit/pan, navegação, download, editar e menu.
- [x] Contador só em variações; nenhuma galeria repetida ao escolher origem.
- [x] Canvas aprovado preservado: pincel, borracha, tamanho, desfazer e zoom.
- [x] Chip de área editável/removível, sem código no texto.
- [x] Proteção de envio Image off; rejeição preserva área e recupera texto sem sobrescrever texto novo.
- [x] Preferências do editor e reset de calibragem acessíveis no menu.

## Validação e limite

Integração real do Open WebUI 0.12.0 exercitada em ambiente descartável com imagens sintéticas e backend de texto sem modelo. Preservação de pixels, fontes, canvas, ledger, auth/ownership e regressões são verificáveis localmente. Precisão da localização visual, adesão semântica e desempenho dos modelos no RTX 5090 são validação de inferência, não uma promessa baseada em testes sem pesos.

Inpainting/segmentação com novos checkpoints e nós anti-drift eram alternativas de pesquisa, não foram adicionados indiscriminadamente. A solução implementada usa grounding do modelo já configurado, máscara manual segura e composição final, sem trocar o stack.

Runbook atual: [runbook-skyrig-1.15.md](runbook-skyrig-1.15.md). Instalar apenas a última versão é suficiente.
