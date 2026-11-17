# PRD: gerenciar IA local pelo desktop

Este documento define o redesenho do Pepito’s AI Hub para Windows. O objetivo é instalar e operar uma stack local sem exigir terminal, mantendo as proteções do motor Python existente. O [PRD de instalações](PRD_INSTALLATIONS.md) detalha os fluxos de arquivos; o [roadmap](ROADMAP.md) separa esta entrega de propostas futuras.

## Público e problema

Você escolhe um perfil e uma pasta; o Hub provisiona Ollama, Open WebUI, ComfyUI, modelos e workflows. Hoje, a navegação divide decisões relacionadas entre telas e torna ações de manutenção difíceis de encontrar. A nova interface reúne essas ações na instalação correspondente e mostra o efeito antes de executá-las.

O público inicial usa Windows 10/11 x64 com NVIDIA. O produto continua local, sem conta do Hub, servidor remoto ou telemetria adicionados por este redesenho.

## Decisões de produto

- Janela desktop nativa com WebView2, React, TypeScript e componentes shadcn/ui
- Backend Python existente como única autoridade sobre instalações, processos e arquivos
- Interface em português, com tema escuro, claro e do sistema
- Mascote Claw existente preservado; nenhum novo personagem substitui a identidade
- Open WebUI continua responsável por chat e imagens; o Hub não duplica esse cliente
- Perfis e manifests continuam declarativos, com versões e revisões fixadas
- Estado desconhecido aparece como desconhecido, nunca como sucesso ou capacidade zero
- Ações destrutivas exigem confirmação contextual e nova validação no backend

## Navegação

| Área | Pergunta respondida | Conteúdo e ações |
| --- | --- | --- |
| Visão geral | O que está selecionado e funcionando? | Instalação ativa, saúde dos serviços, VRAM observada, iniciar/parar/reiniciar, abrir chat, operação atual |
| Instalações | Onde estão meus arquivos e como mantê-los? | Nova instalação, cadastrar existente, alternar, abrir pastas, localizar movida, verificar, reparar, esquecer, excluir |
| Modelos e perfis | O que cada perfil utiliza? | Catálogo pesquisável, filtros, identificadores, provedores, requisitos, licenças e composição dos perfis |
| Atividade | O que está acontecendo ou falhou? | Eventos reais da sessão, progresso disponível, resultado e diagnóstico do ambiente |
| Configurações | Como personalizar e autorizar downloads? | Tema, Hugging Face, informações da aplicação e comportamento de encerramento |

**Nova instalação** abre um fluxo guiado, não outra árvore de menus. Perfil, local e revisão são etapas da mesma decisão.

## PRD da instalação inicial

O fluxo deve manter os dados preenchidos enquanto você revisa as etapas:

1. Escolher um perfil com resumo e compatibilidade observada
2. Escolher local padrão, portátil ou personalizado
3. Usar seletor nativo ou digitar o caminho personalizado
4. Revisar perfil, destino e estimativa de espaço antes de instalar
5. Resolver conflitos externos com uma política explícita, quando existirem
6. Acompanhar eventos e o resultado real da operação
7. Abrir o Open WebUI quando o backend declarar o destino disponível

Requisitos de aceitação:

- Não iniciar instalação ao selecionar um perfil ou pasta
- Explicar incompatibilidade de hardware e falta de espaço
- Indicar estimativas incompletas, sem inventar tamanhos
- Impedir comandos concorrentes incompatíveis
- Não oferecer cancelamento de download se o motor não consegue cancelar com segurança
- Preservar uma instalação incompleta para verificação ou reparo posterior
- Não encerrar silenciosamente o aplicativo durante uma operação ativa

## PRD da operação diária

A Visão geral deve priorizar a instalação ativa, não métricas decorativas. Os estados de serviço vêm do backend; a tela não deduz prontidão apenas de uma porta aberta.

Requisitos de aceitação:

- Iniciar, parar e reiniciar utilizam o controlador existente
- Abrir chat consulta o destino autorizado pelo domínio
- Ações sem instalação selecionada ficam indisponíveis com orientação para instalar ou cadastrar
- Progresso numérico aparece somente quando a operação informa uma medida
- Falhas continuam visíveis com orientação, mesmo depois da próxima atualização de status
- Atualizações não sobrepõem requisições nem roubam o foco dos formulários
- Verificação não altera arquivos; reparo exige uma ação separada

## PRD do catálogo

O catálogo informa o que está disponível nos manifests embarcados. Ele não afirma que todo modelo listado já foi baixado.

Requisitos de aceitação:

- Pesquisar nome e identificador de modelos
- Mostrar provedor, arquivo ou referência, licença e tamanho quando conhecidos
- Mostrar composição dos perfis e requisito de VRAM
- Diferenciar catálogo de inventário instalado
- Não mostrar botões de download, edição de perfil ou importação sem implementação correspondente

## PRD do modelo de chat da instalação

Cada instalação usa o modelo de chat principal do perfil, um dos alternativos que o perfil declara, ou um modelo importado. A escolha fica em `<raiz>/state/chat-model.yaml` e muda só o chat; os modelos de imagem continuam os do perfil.

Requisitos de aceitação:

- Opções extras de chat só aparecem quando declaradas pelo perfil; a seleção troca para a opção declarada e pode voltar ao padrão do perfil
- Importar aceita `usuario/modelo:tag` do Ollama ou `hf.co/usuario/repositorio:quant` do Hugging Face, exige uma tag explícita e recusa `:latest`
- Trocar ou importar salva a escolha e executa o reparo, que baixa o modelo e configura o Open WebUI; se o reparo falhar, a escolha anterior volta
- Modelos importados não são verificados por digest; o diálogo diz isso e lembra o limite de VRAM
- Remover um importado apaga os arquivos do Ollama e exige que ele não esteja em uso
- Confirmar antes de baixar, mostrando o tamanho conhecido e um aviso sobre conteúdo sem censura

Carregar perfis por uma pasta `addons` e importar modelos de imagem (checkpoints, LoRAs) continuam propostas separadas: exigem workflows, validação de schema e política de confiança próprios.

## PRD das configurações e credenciais

O login do Hugging Face deve acontecer sem colar credenciais no terminal ou no chat. O campo de token fica mascarado e é limpo após uso. A aplicação valida a conta antes de salvar pelo mecanismo DPAPI do Windows.

Requisitos de aceitação:

- Consultar identidade e acesso ao modelo gated
- Explicar que aceitar a licença acontece no Hugging Face
- Remover a credencial local por ação explícita
- Informar a precedência de `HF_TOKEN` sobre a credencial salva
- Não registrar token em atividade, URLs, mensagens de erro ou preferências do navegador
- Não prometer OAuth sem um client ID registrado
- Aplicar tema sem reiniciar o motor ou os serviços

## Acessibilidade e estados de interface

Todos os fluxos precisam funcionar por teclado. Diálogos devem conter o foco, fechar com Escape quando seguro e devolver o foco à origem.

Critérios visuais e de interação:

- Controles com rótulos, foco visível e descrição nas ações somente com ícone
- Confirmação destrutiva com caminho legível, sem truncar o destino essencial
- Conteúdo utilizável em janela estreita e com zoom
- Tema claro e escuro com contraste legível
- Respeito à preferência de movimento reduzido
- Estados distintos de carregamento, vazio, indisponível e erro
- Datas e números formatados para português
- Nenhuma métrica, instalação ou mensagem de sucesso fictícia

## Segurança e limites

A interface conversa com um servidor restrito ao loopback e autenticado por sessão. O renderer não pode executar comandos arbitrários, escolher métodos Python ou abrir qualquer arquivo do sistema por uma API genérica.

A exclusão permanece limitada a instalações reconhecidas, com proteção contra caminhos perigosos e links para outros locais. Serviços externos não são encerrados sem passar pela política de conflitos existente.

Esta entrega não adiciona suporte Linux/macOS, multi-GPU, atualizações automáticas, assinatura de código, backup integral, rollback ou um novo motor de geração. O hardware físico da Capella continua dependendo da validação na RTX 5090.

## Evidência exigida para entrega

A entrega precisa demonstrar o renderer real e a janela nativa, além de testes do backend. Os fluxos de cadastro, mudança de pasta e exclusão devem usar dados temporários controlados, nunca apagar instalações pessoais para validar uma tela.

Registre os comandos executados e limites em [validação desktop](testing/desktop-ui-checklist.md). Diferencie prova do gerenciamento de instalações de prova de inferência com GPU.
