# Arquitetura do Hub desktop

O Hub usa uma janela Windows com renderer React e conserva o motor Python de instalação e runtime. A interface não executa comandos do sistema diretamente. Os requisitos de produto estão no [PRD](PRD.md), e a distribuição está em [BUILD_WINDOWS.md](../BUILD_WINDOWS.md).

## Responsabilidades

| Camada | Local | Responsabilidade |
| --- | --- | --- |
| Renderer | `desktop-ui/` | Navegação, formulários, diálogos, tema, catálogo e apresentação de estado |
| Janela nativa | `src/local_ai_stack/desktop/` | pywebview/WebView2, ciclo de vida da janela e servidor local |
| Transporte local | `src/local_ai_stack/desktop/` | Assets, autenticação de sessão, validação de requisições e comandos permitidos |
| Aplicação | `hub_application.py` | Seleção, instalações conhecidas, comandos, exclusão, troca e eventos |
| Coordenação da stack | `stack_application.py`, `first_run.py` | Instalação, reparo, inicialização e composição dos domínios |
| Provisionamento | Provisioners Ollama, ComfyUI e Open WebUI | Aquisição, versões, ambientes e markers |
| Runtime | Controller, launcher, registry e jobs | Readiness, identidade de processos e parada segura |
| Dados declarativos | `profiles/`, `manifests/`, `workflows/` | Requisitos, modelos, artefatos, versões e graphs |

A TUI existente permanece no código e na suíte que exercita o motor. Ela não é o entrypoint padrão da distribuição desktop. O redesign não reescreve provisioners para JavaScript.

## Comunicação entre renderer e Python

O servidor HTTP usa loopback IPv4 e porta efêmera. Uma credencial aleatória por processo autoriza chamadas de API; checagens de Host e Origin restringem a origem das requisições. Não há endpoint remoto, CORS permissivo ou despacho livre de métodos Python.

O renderer recebe projeções serializadas e envia comandos nomeados. Caminhos, enums e timestamps cruzam a fronteira como dados, nunca como objetos executáveis. O backend continua responsável por recusar operações concorrentes, destinos inválidos e exclusões perigosas.

O modo de desenvolvimento `--browser` usa o mesmo transporte e backend da janela nativa. Não é um servidor para acesso por celular ou pela rede.

## Estado e eventos

A autoridade sobre instalações continua no domínio existente:

- `<root>/state/config.yaml` descreve a instalação daquela raiz
- `state/hub-selection.yaml` na raiz padrão registra seleção, instalações conhecidas e histórico resumido
- Saúde, prontidão, espaço livre e VRAM são observações, não flags persistidas como verdade
- O renderer acompanha o snapshot da operação e eventos reais; a lista de eventos da sessão é limitada
- Preferências visuais não podem mudar o perfil instalado nem a propriedade dos serviços

A interface faz atualizações sem sobrepor requisições. Um erro de transporte não significa que a instalação parou; a recuperação exige consultar o estado do backend novamente.

## Comandos e proteção de arquivos

O bridge oferece operações explícitas: instalar, planejar, estimar, iniciar, parar, reiniciar, abrir chat, verificar, reparar, cadastrar, localizar, alternar, esquecer e excluir. Abrir pastas usa raízes reconhecidas e categorias permitidas; não existe um comando genérico para abrir qualquer path.

O diálogo de exclusão mostra o destino completo e exige confirmação. O domínio refaz as verificações antes de apagar. Referências antigas só são removidas quando o cadastro ou a troca para o novo destino conclui com sucesso.

O backend não transfere a decisão de segurança para botões desabilitados no React. A mesma chamada inválida enviada diretamente à API precisa ser recusada.

## Concorrência e encerramento

Uma operação de mutação ocupa o coordenador existente. Downloads e provisionamento continuam nos workers do motor; a interface observa resultados em vez de executar instalação dentro do thread da janela.

Fechar a janela durante uma operação ativa é recusado. Não há cancelamento seguro genérico nesta versão. Fechar o Hub fora de uma operação não equivale a pressionar **Parar**; serviços já iniciados podem continuar em execução conforme o runtime existente.

## Credenciais Hugging Face

O desktop reutiliza `huggingface_auth.py` para validar conta e acesso gated. O token salvo usa DPAPI do Windows; variáveis de ambiente conservam sua precedência. O renderer não guarda credenciais do Hugging Face em preferências, histórico ou URL.

OAuth depende de um client ID registrado. A nova interface não substitui essa dependência por um login fictício.

## Chat e imagens

O caminho de inferência permanece Open WebUI para Ollama e ComfyUI. O Hub abre o destino autorizado pelo domínio, sem construir URLs de serviços a partir de entradas livres do renderer.

Capella (perfil 5.2.0) usa Qwen3.8 27B Q5_K_M no chat, sem variante de chat extra, e Qwen-Image-2.1 INT8 ConvRot (workflows `qwen_image_2_1_t2i` e `qwen_image_2_1_edit`) nas imagens. A troca de personagem usa só Qwen: `reference`, `marked`, `segment` e `depth`, sem passo `scene`. O Hub grava `pepito-image-stacks.json` no diretório de dados do Open WebUI e o filtro de imagem aplica o workflow de cada modelo; os botões "Alta qualidade", "Arte digital" e "Fotorrealista" são filtros toggle que o filtro de imagem lê em `filter_ids`. Só Arte digital ligado escolhe arte; só Fotorrealista ligado escolhe fotografia; ambos desligados ou ligados significam sem preferência, seguindo o pedido e, numa edição, o estilo da fonte. A quantidade de imagens pode ser pedida a cada turno. Os registros do seletor levam a versão do perfil no nome, e presets que o perfil não declara mais são desativados. Sua política de imagem solicita descarregar o Ollama e liberar o ComfyUI após a geração. Isso não substitui medições de VRAM e validação na RTX 5090.

Cada entrada de modelo no stack inclui `num_ctx` quando declarado nos parâmetros de chat do perfil, evitando que a chamada do roteador peça um contexto diferente e provoque recarga do Ollama. As edições Qwen pré-escalam a fonte conforme o workflow do perfil.

O botão "Buscar referências" (`pepito_image_refs`) é anexado a todo preset com workflow de edição quando o perfil tem o roteador de imagem e a pesquisa web; na Capella, isso inclui o registro principal. O filtro de imagem escolhe o `ref_boost` das edições locais de uma imagem. A ordem de prioridade é: um valor das Valves do usuário, uma correção após "não mudou" ou o nível do roteador ajustado por um viés por usuário, que aprende com as variações escolhidas e é guardado em `DATA_DIR/pepito-fidelity.json`. Os detalhes estão em `docs/testing/image-tool-chat.md`.

O prompt inicial da Capella distingue texto de imagens realmente criadas pelo sistema: o modelo não cria imagens, não promete geração nem inventa links ou resultados, e só pede para ligar o botão Imagem quando o sistema diz que ele está desligado. O prompt é semeado no primeiro registro; uma edição do administrador continua preservada, e um texto idêntico ao semeado por uma versão anterior do Hub (`SUPERSEDED_SYSTEM_PROMPTS` em `open_webui_models.py`) é trocado pelo atual. Com Imagem ligado, um turno que o roteador decide como conversa recebe um contexto que diz que o botão está ligado. Antes de agir, o filtro reconcilia a resposta do roteador (`reconcile_decision`): uma pergunta de esclarecimento vira a resposta do turno, e um plano completo marcado como conversa é executado. Toda pergunta do sistema fica pendente no registro do chat por 30 minutos, e uma resposta curta executa o pedido original com a informação dada. Pedidos que mudam a imagem inteira seguem sem área local (`region_skipped`). Trechos `['...']` e `[Ignorar: ...]` são retirados antes do roteador e do modelo e ficam só no trace (`ignored_text`). Nas edições com área, a emenda mede o desvio de cor do modelo num anel fora da área, remove-o com um decaimento para dentro e entra no original por uma rampa interna; fora da área a imagem continua idêntica à origem, e o evento `preservation` registra o degrau antes e depois. Com `runtime.preload_chat_model`, o Hub carrega o modelo de chat em segundo plano depois de iniciar os serviços, e `runtime.chat_keep_alive` vira `OLLAMA_KEEP_ALIVE` do Ollama. Antes de cada spawn, o launcher escreve um cabeçalho UTC nos logs de stdout e stderr do serviço e em `logs/hub-events.log`. O controlador registra `readiness elapsed=...s`, contado com relógio monotônico desde o lançamento até a resposta de saúde (ou a falha), em `hub-events.log`, no relatório de início e no detalhe de atividade. Esse registro nunca vai para o log do serviço já em execução: o processo filho escreve por um handle herdado, com posição própria, e sobrescreveria a linha. Se o processo já existia, o intervalo mede apenas a espera atual; as escritas desses marcos são best-effort.

## Empacotamento

O frontend compila uma vez durante o build. PyInstaller embarca os assets estáticos e o runtime Python. `PepitosAIHub.exe` abre a janela sem console; `PepitosAIHub-CLI.exe` usa o mesmo bundle para diagnóstico textual.

O ícone Windows deriva do frame Claw existente em `assets/pets/claw/idle/01.txt`. O runtime WebView2 pertence ao sistema; Node.js e ferramentas de desenvolvimento não são pré-requisitos do computador de destino.

## Decisões mantidas e evolução

Perfis continuam declarativos; o runtime não instala silenciosamente; provisioners continuam idempotentes. Novas capacidades, como addons, inventário de modelos e atualização automática, exigem contratos próprios antes de entrar na interface. Consulte o [planejamento](ROADMAP.md) para dependências e critérios.

## Filtro 0.23: correções do pod e edição direta do prompt

- Redesenho usa Qwen por padrão nos dois presets Capella, após a avaliação do laboratório. O canvas fica entre 1 e 2 MP. Uma edição que antes era de imagem inteira permanece assim: corrigir a proporção não pode transformar as edições de tamanho aprovadas em uma colagem por área. O arredondamento à grade não cria preenchimento transparente; os resultados do Qwen voltam às dimensões de origem quando não há reenquadramento deliberado.
- Área pintada é vinculada ao texto normalizado, sem notas ignoradas, tanto ao armar quanto ao consumir. Uma área com texto diferente é descartada após um aviso; uma área nova substitui a anterior.
- Propósitos montados pelo filtro a partir de uma receita não são rejeitados pelo limite do texto do roteador. Falhas internas não viram perguntas impossíveis de responder; a repetição tem resolução explícita.
- Nas edições locais, recorte automático só quando área mais contexto cobre menos de metade da imagem. A silhueta experimental foi removida após as avaliações. Um veredito ambíguo só segue globalmente quando a chamada de visão informa explicitamente uma pessoa e confiança de pelo menos 0,8; ausência da contagem não prova sujeito único.
- Na troca, Krea aparece primeiro, sem desligar o Qwen. A análise do compositor é limitada a 2 MP, com aplicação da máscara aos pixels de tamanho cheio. Balões/textos usam máscara de união, para uma detecção vazia não quebrar o SaveImage; proteção por detecção passa a ser o padrão e pode ser desligada nas Valves.
- O menu Prompt consulta uma receita por imagem, autenticada e limitada ao dono do chat. O usuário pode editar o prompt e armar uma regeneração de uso único, expirada em cinco minutos. O filtro consome o pedido sem o roteador e aplica a salvaguarda de idade ao prompt editado e às palavras da receita original. Regerar igual sorteia a semente; prompt editado preserva a semente para comparar mudanças de texto.
- Imagens enviadas e geradas compartilham o visualizador. Enviadas sem receita têm apenas Download. Arrasto funciona em qualquer zoom; o autocomplete de colchete não envia a mensagem, e CSS Highlight atenua as notas sem modificar o texto.
- Os laboratórios públicos compartilham `scripts/lab_review.html` por meio de `scripts/lab_common.py`, com base explícita, escala relativa, prompt por variante e notas isoladas por laboratório/rodada. Culpa do prompt exclui a variante do julgamento do método, preservando a anotação exportada.

## Filtro 0.24: regerar com a mesma semente

Toda imagem gerada tem "Regerar (mesma semente)" no menu ⋯ e no painel Prompt (SPEC-317). A receita gravada roda de novo, sem o roteador, com o prompt e a semente gravados: em edições a do `patch`; na troca, a do turno, com um número fixo por etapa (`SWAP_JOBS`). Receita sem semente recebe 409 e nada é enviado. "Regerar (nova semente)" é o antigo "Regerar igual".

## Filtro 0.25: ver sem colagem e revisão do prompt (lab)

Nas edições com área, a saída do modelo antes da colagem fica guardada com a receita (só o dono lê) e aparece em "Ver sem colagem"; "Refazer na imagem inteira" repete prompt e semente sem área (SPEC-318). O filtro também traz o passo de revisão do prompt (`prompt_revision_payload`: decisão do roteador congelada, fonte e área contornada, regras do estudo de 14/11), usado só pelo lab LAB-03 até a avaliação; a ligação no chat é a CHAT-14.

## Filtro 0.26: duas versões por edição e saída inteira

Com a Valve `prompt_pair` (padrão ligada), toda edição de uma fonte nas partes edit/restage/remove tem o prompt escrito por dois revisores logo após o roteador, com o modelo de chat ainda carregado: E (`prompt_revision_payload`, sem raciocínio) e C (`frozen_route_payload`, com raciocínio). As duas versões rodam com a mesma semente e a mesma força; a galeria de 2 imagens abre lado a lado e `POST /preference` grava a escolhida e a seleciona como base. Troca, deformação, geração nova, regeração e pedidos de N imagens não entram no par. Com `area_paste` desligada (padrão), edições com área entregam a saída inteira do modelo, sem recorte nem colagem (SPEC-319).

## Filtro 0.27: contrato de referências

Com 2 ou mais fontes, `_routed` resolve um mapa de slots (posição física, número no chat, papéis, fins, base) e o prefixo de papéis é compilado uma vez para todo escritor assistido (`compile_reference_prompt`, idempotente; `raw` continua cru). Os escritores que olham imagens recebem todas as fontes na ordem física mais o mapa; Qwen usa `<imageN>` pela posição, Krea ordinais. A receita guarda o prompt final e o regerar não recompila (SPEC-320). O par E/C segue monoimagem até o LAB-04.

## Filtro 0.28: par multirreferência escolhido após LAB-04

Com `prompt_pair` ligado, edições compatíveis com duas ou três fontes passam a gerar V2 primeiro (`prompt_writer_payload`, visão sem raciocínio, mais envelope compilado) e V3/E depois (`prompt_revision_payload`, sem raciocínio, mesmo contrato de slots). Ambas usam as fontes físicas ordenadas, a mesma semente e receitas próprias. Uma fonte mantém E/C. Troca, deformação, gerações novas, regerações e contagens múltiplas explicitamente pedidas preservam as exclusões do par. Falha do primeiro escritor usa o prompt do roteador; falha do segundo entrega só a primeira versão. Salvaguarda roda sobre ambos os prompts finais antes de geração.

Escolha para validação de fluxo, não vencedor universal: R3 Krea e R4–R7 do LAB-04 foram invalidados pelo usuário. O pacote não roda novos labs. Roteiro único: `docs/testing/pod-fluxo-final.md`. O lançamento depende de aprovação do teste real no pod e autorização do dono.

## Filtro 0.29: correções da análise do pod de 15/11

- **Troca:** a variação 1 é a do LAB-04 (R1/R2 Qwen V1/V2, mesmas fontes do teste): prompt do escritor de visão (`prompt_writer_payload`) com o contrato de referências, escrito em `prepare_swap_job` antes de descarregar o Ollama, desenhado inteiro pelo grafo Qwen de duas imagens em ~1 MP na proporção da cena (`swap_whole_canvas`), sem compositor. A variação 2 é "Qwen com marcação", composta com a marcação da personagem. "Qwen com marcação" só roda para regerar receitas antigas. No pod, a personagem colada tinha metade do detalhe da cena (Laplaciano região/entorno 0,58 contra 1,22–1,45 no lab).
- **Remoção de roupa** (capa, chapéu, camisa) vai para a edição geral Krea turbo (`worn_removal`); a remoção Raw fica para pessoas e objetos. No pod, a Raw tirou a capa junto com os braços.
- **Forma:** palavras de proporção (feminino, curvas, magro, cintura…) contam como mudança de forma, e mudança de forma vai para a ordem de métodos (Qwen) também com escopo global. No pod, "corpo mais feminino" foi para o Krea e nada mudou (dentro 5,53, fora 5,67).
- **Escurecimento:** edições `edit`/`remove` de uma fonte, sem colagem, têm cada canal RGB mapeado (média e dispersão) de volta à fonte fora da área, ou na imagem toda sem área (`tone_match`). Não corrige quando o pedido fala de luz/cor, quando a referência mudou mais que 12/255 (mudança global) ou quando a deriva é menor que 0,5 nível. No pod, as edições escureciam 1–3 níveis cada (−6% em quatro edições).
- **Hotfix do pod de 15/11 (23:41):** na troca, a personagem nova leva a própria roupa (o propósito "identity only" do LAB-04 fez o escritor manter a capa verde da que saía); "capa"/"capa de fundo" é peça de roupa para o roteador, não o fundo; numa edição, um formato dito só pelo roteador não muda o quadro da fonte (ele pediu "square" e a cena virou 1408×1408).
