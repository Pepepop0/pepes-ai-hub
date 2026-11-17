# PRD: encontrar e manter instalações

Este documento define as ações do menu **Instalações**, incluindo abrir arquivos, excluir dados e cadastrar uma pasta movida. O objetivo é preservar o vínculo entre o registro do Hub e os arquivos reais. Ele complementa o [PRD desktop](PRD.md).

## Vocabulário e autoridade

Uma instalação é uma raiz com configuração reconhecida pelo Hub, perfil conhecido e estrutura própria de aplicativos, modelos e estado. Uma entrada na lista não prova que seus arquivos ainda existem.

| Conceito | Significado |
| --- | --- |
| Ativa | Destino usado pelos comandos de iniciar, verificar e reparar |
| Em execução | Serviços observados em execução pelo backend |
| Ausente | Pasta registrada não encontrada; não significa exclusão do registro |
| Cadastrar | Validar uma pasta existente, registrá-la e selecioná-la sem baixar novamente |
| Localizar movida | Validar o novo caminho e substituir o registro anterior somente após sucesso |
| Esquecer | Remover uma referência da lista, sem apagar arquivos |
| Excluir | Apagar os arquivos da instalação validada e depois remover sua referência |

## Abrir a pasta da instalação

A ação deve abrir o Explorador no caminho completo da instalação escolhida. Atalhos para modelos e logs usam somente subpastas reconhecidas dessa raiz.

Critérios de aceitação:

- Aceitar apenas raízes conhecidas pelo Hub
- Informar quando a pasta desapareceu
- Não criar pastas ao tentar abri-las
- Não interpretar caminhos como comandos ou argumentos de shell
- Não permitir acesso fora da instalação por travessia de diretórios ou links

## Cadastrar uma instalação existente

O cadastro reutiliza arquivos existentes; ele não converte qualquer pasta em instalação.

Fluxo:

1. Selecionar **Cadastrar existente**
2. Escolher ou digitar a pasta raiz, não a pasta `models` isolada
3. Validar `state/config.yaml`, perfil e destino
4. Se necessário, pedir consentimento para parar a instalação atual
5. Registrar e selecionar apenas depois de uma troca segura

Critérios de aceitação:

- Pasta inválida mostra motivo e não altera a seleção atual
- Repetir o cadastro não cria entradas duplicadas
- O cadastro não inicia downloads
- Uma stack em execução não é interrompida só porque você escolheu outra pasta
- Falha ao parar serviços mantém a instalação anterior selecionada

## Localizar uma instalação movida

O Hub não deve mover gigabytes automaticamente ao receber um novo caminho. Você move a pasta; a ação **Localizar pasta** atualiza o vínculo depois de validar o destino.

Fluxo:

1. Parar os serviços antes de mover os arquivos pelo Explorador
2. Mover a raiz inteira, preservando `state`, aplicativos e modelos
3. Na entrada antiga, escolher **Localizar pasta**
4. Informar o novo destino e confirmar a troca quando necessário
5. Verificar a instalação; executar reparo separadamente se houver referências inválidas

Critérios de aceitação:

- Conservar o registro antigo se o novo caminho for inválido ou inacessível
- Atualizar a seleção persistida para a raiz nova após sucesso
- Reabrir o Hub sem voltar ao caminho antigo
- Não duplicar a instalação nem copiar seus arquivos
- Não prometer que ambientes Python movidos entre máquinas ou volumes sempre funcionam sem reparo

## Alternar entre instalações

Selecionar uma instalação muda o destino dos comandos, não apenas o destaque visual. A interface consulta o plano de troca e informa qual stack precisa parar.

A troca deve falhar sem mudar a seleção se houver serviços sobreviventes, conflito de endpoints ou erro de persistência. O backend continua responsável por provar a propriedade dos processos.

## Verificar e reparar

**Verificar** examina a instalação ativa e relata problemas e itens que não pôde verificar. **Reparar** executa o fluxo de correção existente depois de uma ação explícita.

O resultado deve diferenciar modelo ausente de modelo não verificável porque o Ollama está parado. Uma verificação não inicia downloads nem transforma ausência de observação em falha confirmada.

## Esquecer uma entrada

Esquecer remove somente o registro. O texto da confirmação deve dizer que os arquivos permanecem no disco.

A instalação ativa não pode ser esquecida sem antes selecionar outra. Uma entrada ausente pode ser esquecida; isso não substitui a opção de localizar sua nova pasta.

## Excluir uma instalação

A exclusão apaga aplicativos, modelos e dados contidos na raiz validada. Isso pode incluir conversas e imagens do Open WebUI. Não existe lixeira ou desfazer garantido pelo Hub.

Fluxo:

1. Consultar o plano de exclusão no backend
2. Mostrar nome, caminho completo e consequência; se os serviços estiverem rodando, avisar que serão parados
3. Confirmar com um clique em **Excluir permanentemente** (ou **Parar serviços e excluir**)
4. Se a pasta já não existir, oferecer **Remover da lista** no lugar da exclusão
5. Revalidar segurança e estado no momento da execução
6. Acompanhar a operação e remover a entrada somente depois do resultado correspondente

Critérios de aceitação:

- Recusar raiz do disco, pasta pessoal, pasta do próprio Hub e destinos protegidos
- Recusar pastas não reconhecidas e árvores com links perigosos
- Não apagar uma instalação que contenha outra instalação registrada
- Não apagar enquanto serviços comprovadamente pertencentes à instalação sobreviverem à parada
- Cancelar o diálogo sem alterar arquivos
- Manter o registro em caso de exclusão parcial e explicar a falha
- Não selecionar outra instalação implicitamente após excluir a ativa
- Nunca confundir esquecer com excluir

## Matriz de estados

| Estado observado | Abrir pasta | Selecionar | Localizar | Esquecer | Excluir |
| --- | --- | --- | --- | --- | --- |
| Pasta válida, inativa | Sim | Com plano de troca | Validar destino | Sim, com confirmação | Com plano e confirmação |
| Pasta válida, ativa | Sim | Já selecionada | Validar destino e execução | Não | Com plano e confirmação |
| Em execução | Sim | Parada explícita quando necessária | Parada explícita quando necessária | Nunca a ativa | Parada explícita e prova de encerramento |
| Pasta ausente | Recusar com orientação | Recusar | Sim | Sim se inativa | Recusar; orientar esquecer |
| Configuração inválida | Pasta conhecida pode abrir | Recusar | Validar nova pasta | Sim se inativa | Recusar |
| Operação em andamento | Leitura permitida quando segura | Bloquear | Bloquear | Bloquear mutação concorrente | Bloquear |

## Validação

Use uma raiz temporária controlada para cadastrar, renomear, reencontrar e excluir. Abra uma segunda sessão do adapter para verificar persistência. Cubra destino inválido, confirmação divergente, pasta externa, conflito de operação e recusa de parada.

A validação dessas transições não exige baixar modelos grandes. Geração e edição de imagens continuam no checklist físico da Capella.
