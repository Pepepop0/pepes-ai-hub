# Validar o Hub desktop sem arriscar instalações pessoais

Este roteiro verifica o aplicativo desktop, o transporte local e o gerenciamento de pastas. Use instalações temporárias reconhecidas pelo Hub para operações destrutivas. Inferência com modelos grandes é uma validação separada: [Capella na RTX 5090](capella-5090-checklist.md).

## Preparação

Compile o frontend e use a `.venv` do checkout, conforme [BUILD_WINDOWS.md](../../BUILD_WINDOWS.md). Reserve uma pasta temporária e um registro global isolado. Não use uma instalação pessoal para provar exclusão, cadastro ou persistência.

## Interface e janela nativa

| Cenário | Resultado exigido |
| --- | --- |
| Abrir o aplicativo sem argumentos | Janela nativa com renderer real, sem terminal como interface principal |
| Backend ainda carregando | Estado de carregamento, sem instalações demonstrativas |
| Abrir cada menu | Conteúdo real, sem botões sem implementação |
| Alternar tema | Cores, controles nativos e foco continuam legíveis |
| Reduzir largura e aplicar zoom | Conteúdo e ações principais continuam acessíveis |
| Navegar por teclado | Foco visível, sequência útil e diálogos com foco contido |
| Abrir seletor de pasta e cancelar | Nenhum caminho alterado e nenhuma instalação iniciada |
| Reabrir aplicativo | Instalação selecionada persistida, sem reinstalação |
| Fechar durante mutação | Encerramento recusado com orientação; operação preservada |
| Ver a versão do Hub | Rodapé da barra lateral e **Configurações › Sobre o aplicativo** mostram `v1.19.0`, o mesmo número de `PepitosAIHub-CLI.exe --version` |
| Escolher perfil no assistente | Cada perfil mostra a versão ao lado do nome (Capella `v5.1.1`, Pepe `v1.2.0`, Pepinho `v1.2.0`); a revisão repete a versão escolhida |
| Instalação criada ou reparada nesta versão | Dashboard e Instalações mostram o perfil com a versão instalada, sem aviso de atualização |
| Instalação anterior às versões de perfil | Sem versão ao lado do nome e aviso `Versão instalada desconhecida. Hub traz vX: use Reparar para atualizar.`; depois de **Reparar**, a versão aparece e o aviso some |
| Reparar sem sair de Instalações | Painel da operação mostra etapa, atividade, progresso disponível e diagnóstico na própria página; ao concluir, o resultado permanece visível e o botão volta a **Reparar** |
| Reparo concluído após falha antiga | Histórico mostra **Reparo: Concluído**, não `succeeded`; a falha anterior fica recolhida como histórico, sem alerta vermelho de falha atual |
| Falha posterior ao último sucesso | **Última falha** permanece visível em vermelho; o histórico não oculta uma falha mais recente |

## Instalações e arquivos

Execute a sequência com uma raiz de teste e registre o resultado de cada etapa:

1. Tente cadastrar uma pasta sem configuração; confira recusa e seleção intacta
2. Cadastre uma instalação temporária reconhecida; confira caminho e perfil
3. Abra pasta raiz, modelos e logs; confira o destino no Explorador
4. Cadastre novamente; confira ausência de duplicação
5. Pare os serviços se houver algum, depois renomeie a pasta pelo sistema
6. Atualize o estado; confira indicação de pasta ausente
7. Tente localizar um destino inválido; confira preservação do registro antigo
8. Localize a pasta movida; confira nova raiz e remoção do vínculo antigo
9. Recrie o adapter/servidor; confira persistência da nova seleção
10. Abra a exclusão e cancele; confira arquivos preservados
11. Abra a exclusão de uma pasta que já não existe; confira que o diálogo oferece **Remover da lista** e não exclui nada
12. Confirme a exclusão da raiz temporária; confira resultado terminal, arquivos removidos e registro atualizado
13. Confira que nenhuma instalação pessoal ou arquivo fora da raiz temporária foi alterado

## Provisionamento e runtime

| Cenário | Resultado exigido |
| --- | --- |
| Wizard com perfil e local | Compatibilidade, destino e revisão coerentes |
| Plano sem executar | Passos reais, sem criar instalação ou baixar modelos |
| Destino personalizado com switch ligado | Selecionar `Desktop` instala em `Desktop\Capella`; revisão mostra a raiz final e não cria pastas; destino recente guarda o pai |
| Destino personalizado com switch desligado | Revisão e instalação usam exatamente a pasta escolhida |
| Pausar e retomar instalação ou conclusão/reparo | **Pausando** até o próximo ponto seguro; **Pausada** somente quando todos os workers pararam; parcial não cresce; retomar continua e verifica checksum |
| Cancelar enquanto pausada | Confirmação explícita, resultado **Cancelada**, parciais e arquivos concluídos preservados; Hub disponível e instalação ativa anterior preservada |
| Controle com ID de operação anterior | Recusa sem afetar a instalação atual |
| Primeira instalação falha sem instalação ativa | Cartão oferece **Tentar novamente** com perfil e raiz originais; recuperação usa outro ID e não cria subpasta duplicada |
| Revisão sem token Civitai | Aviso antes dos downloads, modal com campo protegido e alternativa explícita para arquivos já locais; Hugging Face público continua opcional |
| Token recusado no modal | Erro inline útil, campo limpo e nenhuma credencial inválida persistida |
| HTTP 401/403 ou falha de conexão/disco | Cartão informa causa observada e ação, sem URLs, caminhos privados ou credenciais |
| Espaço incompleto/desconhecido | Texto explícito, sem precisão inventada |
| Perfil incompatível | Orientação de incompatibilidade; preflight continua autoridade |
| Dois comandos de mutação | Segundo comando recusado como ocupado |
| Conflito externo | Decisão explícita e cancelamento seguro do plano pendente |
| Verificar | Problemas e itens não verificáveis separados, sem reparo implícito |
| Reparar | Operação observável com resultado do motor |
| Iniciar/parar/reiniciar | Resultado real e saúde atualizada, sem simulação |
| Abrir Open WebUI indisponível | Recusa útil; não abrir URL inventada |

Os cenários que exigem uma stack completa devem usar uma instalação autorizada e hardware compatível. Não marque inferência como validada porque um teste controlado de provisionamento passou.

## Autenticação e segurança

| Cenário | Resultado exigido |
| --- | --- |
| API sem bearer | Recusa, sem dados nem mutações |
| Origin/Host estrangeiro | Recusa, sem CORS permissivo |
| Path traversal em assets ou pastas | Recusa, sem leitura fora do escopo |
| Comando não permitido | Recusa; nenhum despacho arbitrário |
| Payload inválido ou excessivo | Erro controlado, sem traceback ou execução |
| Pasta desconhecida ou protegida | Recusa de abertura privilegiada/exclusão conforme contrato |
| Token Hugging Face, Civitai, Brave, Unsplash ou Pexels inválido | Erro útil sem expor token e sem substituir credencial válida |
| Remover credencial com variável de ambiente correspondente | Explicar que o ambiente continua tendo precedência |
| Pesquisa web: alternar, salvar SearXNG e recarregar | Estado persistido; endereço normalizado; campos de chave vazios e apenas o estado configurado visível |
| Salvar/remover Unsplash ou Pexels | Valor protegido por DPAPI e ausente da resposta; aviso para reiniciar; somente o processo Open WebUI recebe a chave |
| Reload do renderer | Recuperar sessão local sem expor credencial na barra de endereço |

## Distribuição

Execute o build Windows completo e abra `PepitosAIHub.exe`. Use `PepitosAIHub-CLI.exe --smoke` para conferir recursos e `--version` para a versão. Copie a pasta completa para fora do checkout quando validar uma release.

A presença dos assets no bundle não prova que WebView2 abriu. Observe a janela e uma interação real. Não publique URLs com o bearer temporário nem capturas com credenciais.

## Registro desta entrega (12/10/2026, RTX 5070, sem downloads de modelos)

Executado na branch `desktop-ui-shadcn` com um registro global isolado (`PEPITOS_AI_HUB_ROOT` e raízes temporárias), sem tocar nas instalações pessoais:

- **Renderer**: `npm run build` com `tsc --noEmit`, sem erros
- **Cadastro**: pasta sem configuração recusada; duas instalações reconhecidas cadastradas; recadastro sem duplicar
- **Pasta movida**: renomear pelo sistema mostrou **Pasta não encontrada**; destino inválido recusado com o registro antigo preservado; **Localizar pasta movida** gravou a nova raiz em `hub-selection.yaml` e a recarga manteve a seleção
- **Exclusão**: cancelar preservou os arquivos; confirmação divergente bloqueada no renderer e recusada pela API (`confirmation_required`); confirmação exata apagou a pasta e removeu o registro
- **Abrir pasta**: o Explorador abriu exatamente a raiz da instalação ativa
- **Seletor nativo**: diálogo do Windows abriu com o título correto; cancelar manteve o caminho digitado
- **Assistente**: compatibilidade (Capella recusada por VRAM insuficiente na 5070), estimativa de espaço e plano simulado real, sem iniciar instalação
- **Verificação**: somente leitura, problemas listados sem reparo implícito
- **Temas e largura**: escuro, claro e 720 px utilizáveis
- **Janela nativa**: `python -m local_ai_stack` abriu WebView2 com o ícone Claw; fechar pediu confirmação explicando que os serviços continuam; OK encerrou com código 0
- **Bundle**: PyInstaller gerou `PepitosAIHub.exe` (sem console) e `PepitosAIHub-CLI.exe`; `--smoke` do CLI retornou `ok: true` com `desktop-ui/dist/index.html` embarcado

Defeitos encontrados e corrigidos durante a validação:

- Relocar uma instalação personalizada movida fisicamente persistia a raiz antiga gravada no `config.yaml` movido; corrigido em `HubApplicationAdapter._target`, com regressão em `tests/test_installation_atomicity.py`
- Recarregar a janela em qualquer menu diferente da visão geral retornava erro JSON por causa de `?page=`; o servidor aceita query só em assets estáticos, com regressão em `tests/test_desktop_security.py`
- O assistente bloqueava toda instalação nova porque a simulação marca como pendentes etapas que dependem de downloads anteriores; agora só etapas não executáveis bloqueiam
- Cada leitura de `/api/state` levava ~5 s (duas sondagens de serviços por leitura), atrasando toda resposta da UI; agora um observador em segundo plano sonda uma vez por ciclo e a leitura responde em ~2 ms, com regressão em `tests/test_desktop_state.py` (o estado acompanha a troca de instalação sem esperar a próxima sondagem)
- A revisão bloqueava toda instalação porque a etapa `check` aparece sempre como não executável pelo executor (ela roda como preflight no início da instalação); agora só etapas realmente não executáveis bloqueiam, e a `check` é explicada na revisão

Auditoria de usabilidade e correções (mesmo dia):

- Botões que consultam o backend mostram ocupado imediatamente (`Consultando…`, `Trocando…`, `Excluindo…`)
- Confirmação de troca só pede autorização para parar serviços quando o plano exige; cadastrar/localizar pedem após `stop_required`
- Toasts acessíveis para sucesso e falhas fora de diálogos
- Instalações inativas aparecem como **Disponível** (não verificadas), e a ativa incompleta ganha o estado **Instalação incompleta** com **Verificar** e **Concluir instalação**
- Textos de marketing e decoração substituídos por informação real; indicador de conexão fiel; esqueletos no primeiro carregamento
- Animações: transição de página, indicador da barra lateral, entrada e saída de diálogos e menus, reordenação da lista, passos do assistente, abas e toasts; desativadas com movimento reduzido
- Foco volta ao controle que abriu o diálogo; menu não reabre após escolher uma ação

CLI para agentes: `hub state`, `hub commands`, `hub run <comando> [--args JSON] [--wait]` e `hub serve --no-open`, com trava de escrita única entre janela e CLI. Guia em [AGENT_WORKFLOW.md](../AGENT_WORKFLOW.md), testes em `tests/test_agent_cli.py`.

Conveniências (mesmo dia), verificadas no navegador contra um backend isolado com quatro instalações e logs reais:

- **Logs**: abas por serviço, últimas 200/500/1000 linhas, modo ao vivo mostrou uma linha acrescentada ao arquivo em até 2 s, copiar, abrir pasta
- **Tamanho em disco**: "Calcular tamanho" por instalação, sem seguir links, parcial após 15 s
- **Endereços dos serviços** na Visão geral, com copiar e abrir quando o serviço está em execução
- **Copiar caminho** (conferido na área de transferência) e copiar o relatório de diagnóstico
- **Filtro** de instalações por nome/pasta e estado a partir de quatro instalações
- **Paleta de comandos** (`Ctrl+K`): "usar capel" + Enter abriu a troca para Capella; ações indisponíveis mostram o motivo
- **Atalhos**: `Ctrl+1…5`, `Ctrl+N`, `Ctrl+L`, `?`; ignorados enquanto se digita em um campo
- **Última página e aba de Modelos** restauradas ao reabrir
- **Janela**: fechar com serviços em execução pergunta Sim (parar e fechar) / Não (fechar e manter) / Cancelar; uma segunda abertura traz a janela existente para frente; tamanho, posição e maximizado são lembrados (`desktop-window.json`). Cobertos por testes; o diálogo nativo com serviços reais rodando ainda precisa de verificação manual
- **Segurança**: "Abrir pasta" passou a recusar junções do Windows também no Python 3.11 (antes só links simbólicos eram detectados)
- **Pré-requisito Git**: numa máquina simulada sem Git, **Nova instalação** mostrou o requisito real, confirmação do pacote fixo `Git.Git`, alternativa de download manual e feedback para reabrir o Hub; testes cobrem Git já presente, `winget` ausente, timeout/falha e consentimento. Nenhum instalador real foi executado nesta validação.
- **Progresso de dependências Python**: `Installing collected packages` do pip não informa avanço por pacote. O parser não atribui mais todo esse período silencioso ao último nome (`transformers`/`open-webui`), e a UI não reutiliza o download histórico de metadados como `0%`. O cartão mostra `pip (Python)`, duração da operação, idade da última atualização e explica a ausência de percentual. Verificado no navegador com um snapshot que preservava intencionalmente `0 B de 23,4 KB` no histórico: a UI exibiu progresso indeterminado, não `0%`.
- **Primeira inicialização lenta (RTX 5090)**: a instalação Capella falhou com `open-webui: process is running but did not become healthy within 120.0s`; o diagnóstico seguinte mostrou o Open WebUI `UP`. O prazo de Open WebUI e ComfyUI subiu para 600 s (a espera termina assim que o serviço responde e falha na hora se o processo fechar), e o cartão mostra o serviço aguardado e o aviso de que a primeira inicialização pode levar minutos.
- **Ollama em instalação limpa (v1.0.1, RTX 5090)**: o processo ainda não respondeu a `/api/version` dentro do limite de 90 s, mas o diagnóstico posterior mostrou Ollama **UP**; a instalação falhou antes de adquirir modelos e iniciar Open WebUI/ComfyUI. v1.0.2 eleva o teto para 600 s e, no retry, continua aguardando um processo gerenciado que ainda não está saudável. Confirmar em SkyRig que o retry conclui a aquisição sem criar outra instalação.
- **Reparo do ComfyUI no SkyRig**: "Concluir instalação" falhava com `comfyui: Could not clone ComfyUI.`. Causa confirmada no próprio SkyRig: o checkout já existia e o reparo roda `fetch`/`checkout`, que recusam árvore suja; o marcador `.pepito-managed.json` que o próprio Hub grava aparecia como `??` no `git status`, e a recusa virava "não clonou" sem causa. Agora: arquivos do Hub no checkout (marcador e variantes, `.venv`) não contam como alteração local; um checkout já no commit fixado não é atualizado; edições reais continuam bloqueando; o diagnóstico nomeia a causa com texto do Hub, sem caminhos/URLs ("Could not update the ComfyUI checkout: ..."). Também: clone com limite de 30 min e remoção de clone interrompido no limite. Smoke real: clone de `v0.34.0` com marcador e `.venv` — a regra antiga recusou como suja, a nova fez `fetch` + `checkout`.
- **Instalação movida (SkyRig)**: `open-webui: process … exited during startup`. A instalação foi criada com raiz `Desktop` e as pastas foram depois movidas para `Desktop\capella`. O `open-webui.exe` que o pip gera grava o caminho absoluto do Python do ambiente e passou a falhar com "Fatal error in launcher" apontando para `Desktop\apps\open-webui\.venv\...`; reinstalar um pacote já satisfeito não reescreve o lançador. Agora a detecção marca o Open WebUI como inválido quando o lançador aponta para outra pasta, e o reparo regenera só esse pacote (`--force-reinstall --no-deps`). Smoke real: venv criada, movida — `launcher_interpreter` leu o caminho antigo e o lançador movido falhou ao executar.
- **Controle por serviço**: menu `⋯` em cada serviço (Parar/Iniciar, Reiniciar, Abrir no navegador, Copiar endereço, Ver logs já filtrado no serviço) e botão **Parar serviços**. Abrir o Open WebUI sozinho sobe o Ollama antes; ComfyUI sobe sozinho; parar um serviço não para os outros; processos que a instalação não iniciou nunca são parados. Verificado no navegador contra backend isolado: o menu chamou `stop_service:open-webui` e o "Ver logs" abriu já no ComfyUI.
- **Prompt de sistema padrão (Capella)**: o perfil declara `models.chat.system_prompt`. O Hub grava esse texto em `params.system` do modelo de chat no Open WebUI **só enquanto o modelo não tiver um prompt**: o que o administrador escrever em Admin → Modelos nunca é sobrescrito; apagar o prompt faz o padrão voltar no próximo início. Confirmado no código do Open WebUI 0.12.0 que `params.system` do modelo vira a mensagem de sistema de cada conversa.
- **Parar o Open WebUI deixava o servidor de pé** (`service-left-running`, SkyRig): a raiz registrada é o lançador `open-webui.exe`; quem atende a 8080 é o Python que ele inicia. A parada agora encerra a árvore provada pelo Job Object, como já era com ComfyUI e Ollama. Teste real (`tests/test_jobs.py`): raiz + processo filho escutando numa porta, Hub reiniciado entre o início e a parada — a porta ficou livre e nenhum processo sobrou.
- **Edição virou imagem nova sem relação** (SkyRig, filtro 0.6.1): "in this immage, i want it massively bigger" — o erro de digitação em "image" e a falta de verbo de edição fizeram o filtro tratar como imagem nova; o FLUX desenhou uma cena aleatória "massiva" e o modelo de chat ainda anunciou que "ampliou" algo. Filtro 0.6.2: "image/imagem" com letra trocada ou duplicada conta como a imagem do chat; pronome + comparativo ("it … bigger", "ela maior") é edição; em imagem nova, o modelo é avisado de que nada da anterior foi alterado. A primeira tentativa ("não fez nada") é a supressão de conceitos do FLUX.2 Klein na edição, não roteamento.
- **Roteamento 2.0 (Capella, filtro 0.7.0)**: com Image ligado, o próprio modelo de chat decide `chat`/`new`/`edit` e escreve o prompt em inglês, numa chamada `/api/chat` com `format` (JSON Schema) antes de descarregar o Ollama. `chat` não gera nada, não descarrega nada e o modelo é avisado de que nenhuma imagem foi feita; `new`/`edit` são feitos pelo filtro (a edição usa a instrução reescrita e só a imagem mais recente). JSON inválido, timeout ou Ollama fora → as regras de palavras decidem com o texto original. Pepe e Pepinho continuam só com as regras. Detalhes e medição em `image-tool-chat.md`. **Falta no SkyRig**: acerto e latência do 27B real, e um turno de cada ação no navegador.
- **Configurações de pesquisa (14/10/2026)**: card real aberto contra o backend isolado em 1440 px e 720 px, sem overflow horizontal; alternar desligou a pesquisa, o endereço `http://localhost:8888` voltou normalizado como `http://localhost:8888/search?q=<query>` após recarregar, sem erro de console. Campos e ações Brave/Unsplash/Pexels ficaram acessíveis; nenhuma chave real foi usada.

Notificações do sistema ao terminar uma operação com a janela em segundo plano ficaram de fora: o WebView2 nega a permissão de notificação por padrão, e um botão que não funciona na janela nativa seria enganoso.

Não validado nesta entrega: instalação completa com downloads, iniciar/parar serviços reais pela nova UI, conflitos externos reais, login Hugging Face com token real e inferência na RTX 5090. Esses itens continuam no roteiro acima e no [checklist Capella](capella-5090-checklist.md).

## Destino protegido e controles de instalação (20/10/2026)

Verificado no navegador com o renderer de produção e o Hub real em raízes temporárias:

- Switch ligado por padrão criou `Desktop\Capella`; desligado usou a pasta exata. Ambas as revisões deixaram o disco intacto.
- Um modelo de 32 MB veio por HTTP loopback. Pausar interrompeu o crescimento do `.part`; retomar concluiu a instalação e o SHA-256 conferiu.
- Cancelar outra instalação enquanto pausada preservou o parcial e a instalação ativa anterior. O cartão mostrou **Cancelada**, os controles foram liberados e o console não registrou erros.
- O instalador do IObit Unlocker foi baixado pelo WinGet e passou na verificação de hash. O BAT foi exercitado com comandos de sistema simulados, sem instalar programas nem reiniciar o Explorer desta máquina.
- Primeira instalação sem perfil ou raiz ativa: HTTP 403 real no servidor loopback exibiu causa e orientação para o Civitai. **Tentar novamente** abriu o modal; uma identidade controlada recusou o token inválido e aceitou uma credencial fictícia, salva com DPAPI real em arquivo temporário isolado. O retry concluiu no perfil e na raiz originais, com outro ID e pausa/retomada funcionais.
- Modal em janela de 720 px: sem rolagem horizontal, conteúdo alto acessível por rolagem vertical; fechar e reabrir manteve os campos de token vazios.

Git, pip, serviços e fatos de GPU estavam controlados pelo harness. Essa prova não valida instalação de dependências reais, inferência ou desempenho na RTX 5090.

