# Capella no Linux — Hub 1.19.0

## Antes de alugar: conta e chave pública, só

Crie sua conta no provedor e cadastre **somente a chave SSH pública** (`.pub`). A chave privada fica no seu Windows: não envie por chat, não copie para o pod e não a inclua no pacote. Não é necessário compartilhar senha, token de API do provedor ou acesso à conta para preparar este bench. Este roteiro não cria aluguel nem autoriza gastos automaticamente.

Quando decidir contratar, escolha **Secure / Secure Cloud, não Spot / interruptível**:

- Linux **x86_64/amd64**, preferencialmente Ubuntu 24.04, com Python **3.11 ou 3.12** e SSH.
- Uma **RTX 5090 de 32 GB**, **64 GB de RAM ou mais** e aproximadamente **150 GB livres** no destino. O mínimo operacional de RAM aceito pelo preflight não substitui esta recomendação de aluguel.
- Driver NVIDIA no host cujo `nvidia-smi` anuncie suporte a **CUDA 13.0 ou superior**; a imagem precisa disponibilizar a GPU ao pod. Não tente substituir o driver do host dentro do contêiner.
- Acesso externo somente ao SSH. **Não publique 8080, 8188 ou 11434** no painel do provedor. Não exponha ComfyUI nem Ollama à internet.

O Hub é 1.19.0; o perfil é **Capella 5.1.1**, com os mesmos checkpoints/workflows (só desliga tarefas automáticas do Open WebUI). A pilha continua Open WebUI **0.12.0**, ComfyUI **v0.39.0**, Ollama **0.33.2** e torch **2.11.0+cu130**. Não há troca de modelo neste roteiro. Windows continua suportado pelo pacote Windows; o TAR abaixo é para o host Linux.

## Atalho: pod para um convidado, em três cliques

Com o pod alugado e a sua chave pública cadastrada nele, dê dois cliques em **`Pod-Convidado.cmd`**, na raiz do checkout. Ele pergunta o que fazer, o host (`<usuario>@<host>`) e a porta SSH, e executa as seções 1 a 3 e o link público abaixo em ordem (`scripts\pod-guest.ps1`):

1. **Subir** (primeira vez):
   - gera o pacote Linux se faltar e confere o SHA256;
   - envia o pacote, confere de novo no pod e extrai;
   - roda `deps`, `prepare`, `check` e `install` (cole o token do Civitai quando pedir) e `start`;
   - abre o **seu** túnel em `http://127.0.0.1:18080` e espera você criar a sua conta admin;
   - roda `start` de novo e abre o link público. Mande para ela o endereço emoldurado.
2. **Reabrir o link**: reabre o seu túnel e o `share`, com o pod já instalado.
3. **Laboratório de troca**: envia `Downloads\swap-lab` e roda os métodos de troca de personagem. Traz os resultados e, **no PC**, roda o compositor sem marcas de corte. Abre `avaliar-troca.html` e `avaliar-composicao.html` ([swap-lab.md](swap-lab.md)). Na primeira vez, cria o ambiente do compositor em `%LOCALAPPDATA%\PepitosAIHub\swap-venv` (torch CPU, alguns minutos), nunca na `.venv` do Hub.
4. **Encerrar**: para os serviços e traz a sessão para `Downloads\capella-pod-sessao-<data>.zip` com `fetch-session`. Só depois disso exclua o pod.

Na primeira conexão, o `ssh` pergunta se você confia na chave do servidor; confira e responda `yes`. O túnel local usa a porta 18080 para não colidir com uma Capella local na 8080. Qualquer falha para o script com a mensagem do passo que falhou, e "Encerrar" nunca diz "pronto" se a sessão não chegou. Testado no Windows com `ssh`/`scp` simulados (a ordem e os argumentos dos comandos, as perguntas e a recusa quando o `fetch-session` falha). Não testado contra um pod real.

## 1. Preparar e transferir o pacote pelo Windows

No PowerShell, use o checkout e sua `.venv` de desenvolvimento já preparados. O builder não depende do diretório atual, não baixa modelos, não reconstrói o renderer e usa o `desktop-ui/dist` de produção já existente. Ajuste os caminhos e os dados SSH fornecidos pelo provedor:

```powershell
$HubSource = "C:\caminho\pepitos-ai-hub"
$HubPython = Join-Path $HubSource ".venv\Scripts\python.exe"
& $HubPython (Join-Path $HubSource "scripts\build_linux.py")

$Release = "PepitosAIHub-Capella-v1.19.0-linux-amd64"
$Archive = Join-Path $HubSource "dist\$Release.tar.gz"
$Receipt = "$Archive.sha256"
(Get-FileHash -Algorithm SHA256 $Archive).Hash.ToLowerInvariant()
Get-Content $Receipt

$SshHost = "USUARIO@HOST"
$SshPort = 22  # troque pela porta SSH do provedor
$Identity = Join-Path $HOME ".ssh\id_ed25519"
ssh -p $SshPort -i $Identity $SshHost
```

Compare os dois SHA256. Na primeira conexão SSH, compare a impressão digital da chave **do servidor** com uma fonte confiável do provedor antes de aceitar. Não use `StrictHostKeyChecking=no`. Depois de conferir e entrar, digite `exit` para voltar ao PowerShell; a chave verificada fica no `known_hosts`. O `fetch-session` exigirá essa confiança já estabelecida e autenticação sem prompt (use o agente SSH local se sua chave tiver senha).

```powershell
scp -P $SshPort -i $Identity $Archive $Receipt "${SshHost}:/tmp/"
ssh -p $SshPort -i $Identity $SshHost
```

Agora, **no terminal remoto Linux**:

```bash
cd /tmp
sha256sum -c PepitosAIHub-Capella-v1.19.0-linux-amd64.tar.gz.sha256
mkdir -p /mnt/pepitos-bench
cd /mnt/pepitos-bench
tar -xzf /tmp/PepitosAIHub-Capella-v1.19.0-linux-amd64.tar.gz
cd PepitosAIHub-Capella-v1.19.0-linux-amd64
```

Use `/mnt/pepitos-bench` apenas se existir espaço suficiente nesse filesystem; pode trocar por outro caminho absoluto dedicado. **`/mnt` não é garantia de persistência**: sem volume persistente anexado e confirmado no provedor, trate tudo no pod como descartável.

O pacote contém `BUILD-MANIFEST.json`, com tamanho, SHA256 e modo de cada arquivo de payload, além das versões declaradas. Os hashes internos cobrem os bytes empacotados, com textos em LF; o manifesto não lista o próprio hash. O recibo externo cobre o TAR.GZ completo. Não há `.venv`, modelos, dados de usuário ou segredos no pacote.

Para escolher outro diretório de saída do builder, use `--output DIRETORIO`; o padrão é `dist` **do checkout**, mesmo executando de outra pasta. Uma saída relativa explícita é relativa ao terminal atual. O builder usa Python 3.11+ e PyYAML da `.venv` do projeto.

## 2. Dependências, preparo e preflight

Os comandos seguintes são **no Linux**, dentro da pasta extraída. `deps` instala pré-requisitos do sistema e exige root em Debian/Ubuntu; use `sudo` se sua sessão não for root. Revise essa instalação explícita antes de executá-la:

```bash
bash scripts/capella-linux.sh deps
bash scripts/capella-linux.sh prepare
bash scripts/capella-linux.sh check /mnt/pepitos-bench/capella-data
```

`prepare` cria a `.venv` do pacote e instala o CLI original em modo editável. Não instala os modelos Capella. `check` é somente leitura: verifica Linux/x86_64, Python, GPU/VRAM, RAM e compatibilidade do driver com cu130. **Preflight aprovado não prova inferência CUDA.** Se recusar, guarde a mensagem e corrija a escolha do host; não force a instalação e não troque as versões fixadas.

Distinção importante: extração, manifesto, `prepare`, `--version` e `hub commands` podem ser avaliados em um Linux sem GPU. Isso valida empacotamento/importação/CLI, **não** disponibilidade da GPU, carregamento dos modelos ou qualidade de edição. O diagnóstico geral `--smoke` do pacote Linux ainda retorna `python_runtime: RuntimeError: no managed Python artifact is pinned`; ele não é considerado aprovado. O roteiro Linux usa o Python 3.11/3.12 do host e seu próprio `check`, que em CPU deve recusar a ausência da GPU.

### Validado antes do aluguel (Docker Linux x86-64, sem GPU)

- Pacote gerado duas vezes com o mesmo SHA256, extraído no Linux e preparado por `prepare`; `--version` informa a versão do Hub.
- `check` recusou corretamente a máquina sem GPU, sem driver CUDA 13 e com pouca RAM, sem criar a pasta de destino.
- Provisionadores reais instalaram o Ollama 0.33.2 Linux conferido por SHA256, o Open WebUI 0.12.0 com a extensão nativa e o ComfyUI v0.39.0 com o nó Krea fixado. Para não exigir GPU, este ensaio usou torch **CPU**; na 5090, o manifesto mantém `torch 2.11.0+cu130`.
- Ollama e Open WebUI iniciaram, responderam pelo túnel SSH local e foram parados por outro processo controlador, sem processos restantes.
- `fetch-session` exportou dados reais do Open WebUI, transferiu por SSH com chave de host verificada e publicou um ZIP conferido, sem banco bruto.

Não validado: CUDA, ComfyUI em GPU, modelos Capella, qualidade das edições e desempenho.

## 3. Instalar e abrir somente pelo túnel SSH

A instalação baixa a pilha e os modelos e consome disco/rede no host contratado. Execute em um terminal SSH interativo:

```bash
bash scripts/capella-linux.sh install /mnt/pepitos-bench/capella-data
```

O instalador pede o token Civitai com **entrada oculta**, apenas para esse processo. Cole-o nesse prompt, nunca em um comando. Alternativa para automação: injete `CIVITAI_TOKEN` pelo mecanismo de **ambiente secreto** do provedor, sem escrever seu valor no histórico, em arquivos ou argumentos. Não use o fluxo de credencial persistida do Windows no Linux. Não envie tokens junto com logs, imagens ou mensagens. Depois da instalação, retire o segredo temporário do ambiente do provedor se não for mais necessário.

```bash
bash scripts/capella-linux.sh start /mnt/pepitos-bench/capella-data
bash scripts/capella-linux.sh status /mnt/pepitos-bench/capella-data
```

Em **outro PowerShell no Windows**, defina novamente `$SshHost`, `$SshPort` e `$Identity` como acima e mantenha este túnel aberto:

```powershell
ssh -N -o ExitOnForwardFailure=yes -p $SshPort -i $Identity -L 127.0.0.1:8080:127.0.0.1:8080 $SshHost
```

Abra **http://127.0.0.1:8080** no navegador do seu Windows. Não use a URL pública do pod para o Open WebUI e não crie túneis para ComfyUI/Ollama. A primeira conta criada em um Open WebUI novo será a administradora: crie-a você, em uma instalação privada.

**Em conta recém-criada, antes de conversar:** volte ao terminal Linux e rode `start` novamente para sincronizar filtros e registros dos modelos com o administrador que acabou de existir:

```bash
bash scripts/capella-linux.sh start /mnt/pepitos-bench/capella-data
```

Recarregue o navegador (Ctrl+F5). Selecione **Capella**, abra um chat novo e ligue **Image** em Integrations. Comece com Buscar referências desligado. Não inicie inferência se ainda houver erro de instalação/sincronização.

O `start` também desliga, no Open WebUI da Capella, as sugestões de continuação (follow-ups) e a geração de tags (o título do chat continua): são duas chamadas extras ao modelo de 27B depois de cada resposta, que podiam carregá-lo ao lado de uma imagem em andamento. A troca vale para instalações já existentes, não só para um banco novo.

Para acompanhar cada turno ao vivo, abra **outra sessão SSH** no pod e deixe rodando:

```bash
bash scripts/capella-linux.sh trace /mnt/pepitos-bench/capella-data
```

Cada linha mostra hora local, chat e turno (8 primeiros caracteres), o evento e seus campos principais: origem do roteador (`model`, `rules` ou `correction`) e tempo/timeout, esperas de GPU, `ollama ps` (quanto do modelo ficou na GPU), parâmetros e duração de cada imagem, métricas de preservação, tokens/s da resposta. Linhas de erro/recusa começam com `!!`. O trace (`ROOT/data/open-webui/pepito-trace/turns.jsonl`) é seguido mesmo após a rotação; `TRACE_LINES=50` antes do comando mostra mais histórico. Ctrl+C só encerra a visualização.

### Link público para um convidado (pod descartável)

Para alguém de fora entrar sem SSH nem script: com os serviços iniciados e **a sua conta admin já criada** pelo túnel, abra outra sessão SSH no pod e deixe rodando:

```bash
bash scripts/capella-linux.sh share /mnt/pepitos-bench/capella-data
```

O comando abre o cadastro no Open WebUI com papel **user** (sem a tela de "aguardando aprovação"), baixa uma vez o `cloudflared` 2026.10.7 conferido por SHA-256 em `ROOT/tools` e cria um túnel rápido da Cloudflare, sem conta, **só para o Open WebUI**. ComfyUI e Ollama continuam fechados. O link `https://<palavras>.trycloudflare.com` aparece emoldurado: mande-o. A pessoa abre, clica em **Cadastrar-se** e usa o chat. Ctrl+C encerra o link; se o comando reiniciar, o link muda.

Enquanto o link está no ar, **qualquer pessoa com ele pode criar conta**. Use só num pod que será excluído depois. O cadastro continua aberto até o pod acabar; antes de excluir, colete a sessão (seção 5). O túnel usa HTTP/2 sobre TCP 443, porque pods costumam bloquear o UDP do QUIC.

Testado no Windows com o mesmo código e o `cloudflared` 2026.10.7 para Windows: o link apareceu, e HTTP e WebSocket passaram pelo túnel até um servidor local. Não testado: o Open WebUI real atrás do link (cadastro, chat ao vivo, imagens) e o binário Linux no pod.

## 4. Cinco pedidos curtos, somente adultos e não explícitos

Use os mesmos cinco pedidos do [runbook 1.15](runbook-skyrig-1.15.md), em sequência, sem repetir toda a bateria antiga:

| # | Pedido | O que observar |
|---|---|---|
| 1 | `faz uma foto de uma mulher adulta de casaco vermelho num café` | Uma imagem, não um par oculto. |
| 2 | `deixa o casaco azul, mantendo o rosto e o fundo` | Região automática segura ou pedido de pintura manual; fora da máscara, preservar pixels e resolução. |
| 3 | `agora faça só um sorriso, sem mudar a roupa` | A instrução nova chega ao modelo; não repete a edição do casaco. |
| 4 | `repita exatamente a última edição` | Repete a receita, sem aumentar fontes nem tentativas. |
| 5 | Remova qualquer área anexada e peça `agora reenquadre em 16:9 horizontal` | Canvas horizontal real; conflito de pose/máscara pede esclarecimento. |

Compare origem e resultado em tamanho cheio. Anote o número do pedido, horário e resultado; não confunda preservação de pixels com cumprimento semântico. Se falhar, pare a sequência e capture a sessão: não gaste cinco novas gerações para reproduzir o mesmo defeito. A aderência do Krea/Qwen, localização visual e desempenho da RTX 5090 só podem ser avaliados com inferência real nesse host.

### Reconferir as correções da 1.17

Na mesma conversa, depois dos cinco pedidos, e com o `trace` aberto na outra sessão SSH:

| # | O que fazer | O que observar |
|---|---|---|
| 6 | Mande uma mensagem de texto **enquanto** uma imagem ainda está sendo gerada | O status mostra "Aguardando a GPU liberar"; o trace registra `gpu_wait`. Depois, `ollama_ps` mostra o modelo inteiro na GPU e a resposta sai rápida (dezenas de tokens/s, não ~5). |
| 7 | `me mostra a imagem 2 de novo` | A imagem 2 volta anexada, sem gerar nada (`show_image` no trace, nenhum `image_job`). |
| 8 | Depois de uma edição, `nada mudou, refaça deixando o casaco verde` | Vira edição com a mudança pedida; não aparece "Uma repetição não pode incluir novas mudanças". |
| 9 | `deixa o casaco dela bem maior e mais longo` | Área automática ampliada e fidelidade menor (`mask_dilation` no trace); a forma muda de verdade. |
| 10 | Uma conversa só de texto logo depois de uma imagem | O modelo não diz "1 imagem gerada." nem descreve imagem inexistente. |

Role o histórico para cima e para baixo: imagens já vistas aparecem na hora, sem recarregar.

Pose de uma personagem copiada de outra imagem **não** foi alterada nesta versão.

### Laboratório de edição (1.18.0)

A comparação histórica E1/E2 pertence ao laboratório de edição do pacote privado e não é executável no bundle público. Para validar o público, use pedidos SFW no chat e o [laboratório Qwen de troca](swap-lab.md). A revisão técnica geral está em [edit-industry-review.md](../research/edit-industry-review.md).

## 5. Coletar e baixar ANTES de apagar o pod

Não encerre o aluguel ainda. Termine a atividade no navegador e, no **Linux**, pare os serviços para obter uma captura estável, depois faça a coleta:

```bash
bash scripts/capella-linux.sh stop /mnt/pepitos-bench/capella-data
bash scripts/capella-linux.sh collect /mnt/pepitos-bench/capella-data /mnt/pepitos-bench/capella-session.zip
```

O `collect ROOT [ZIP]` usa `hub export-session --root ROOT --output ZIP` e retorna um recibo. Sem o terceiro argumento, cria um ZIP com horário dentro de `ROOT/exports`. O arquivo é evidência, **não uma promessa de backup integral/restauração da instalação**.

No **PowerShell do Windows**, use o CLI do **checkout/venv da mesma versão**, não o executável de uma versão antiga:

```powershell
$HubSource = "C:\caminho\pepitos-ai-hub"
$HubPython = Join-Path $HubSource ".venv\Scripts\python.exe"
$SshHost = "USUARIO@HOST"
$SshPort = 22
$Identity = Join-Path $HOME ".ssh\id_ed25519"
$SessionZip = Join-Path $HOME "Downloads\capella-linux-session-01.zip"
& $HubPython -m local_ai_stack hub fetch-session --host $SshHost --port $SshPort --identity-file $Identity --bundle /mnt/pepitos-bench/PepitosAIHub-Capella-v1.19.0-linux-amd64 --root /mnt/pepitos-bench/capella-data --output $SessionZip
```

`--bundle` é a pasta **absoluta remota extraída**, que contém `.venv/bin/python`; `--root` é o destino **absoluto remoto da instalação**. `--output` deve ser um **ZIP local novo**, sem sobrescrever uma captura anterior. Ajuste ambos os caminhos se escolheu outro filesystem. `--identity-file` é opcional se o agente/configuração OpenSSH já selecionar sua chave.

Para compatibilidade segura com clientes SCP antigos, `--bundle` e `--root`
aceitam apenas letras ASCII, números, `/`, `.`, `_` e `-`, sem espaços ou
metacaracteres. Os caminhos deste roteiro já atendem a essa regra.

O `fetch-session` solicita **uma nova exportação remota**, transfere por SCP e só publica o ZIP local após conferir tamanho, SHA256 do arquivo completo e integridade do manifesto interno. Não baixa automaticamente o ZIP manual da etapa `collect`; este fica como captura adicional no pod. Use somente o SSH com host já verificado. Se o comando falhar, preserve o pod e resolva a transferência antes de apagar qualquer coisa.

**O ZIP pode conter prompts, conversas e imagens do usuário. Mantenha-o privado**, inclusive após a limpeza de segredos feita pelo coletor. Compartilhe apenas com quem deve analisar a sessão, por um canal privado; não publique em issue aberta. Aguarde o retorno bem-sucedido do `fetch-session` e confirme o arquivo local antes de encerrar o pod.

O ZIP inclui o trace dos turnos (`data/open-webui/pepito-trace/turns.jsonl` e `turns.jsonl.1`), com a mesma limpeza de segredos dos logs aplicada registro por registro. O trace contém os textos dos seus pedidos e os prompts enviados aos modelos: trate-o com a mesma privacidade das conversas.

O coletor guarda as máscaras disponíveis, inclusive a da última receita de
edição persistida. Máscaras antigas já sobrescritas não podem ser recriadas.
Os limites de segurança rejeitam uma exportação excessiva em vez de truncá-la;
qualquer omissão ou limite de evidência aparece no manifesto.

Por último, encerre o túnel com Ctrl+C e desligue/exclua o pod pelo painel do provedor para interromper a cobrança conforme as regras dele. **Excluir o pod é destrutivo**: sem volume persistente anexado, não conte com a recuperação de `/mnt`, modelos, chats ou imagens. Não exclua antes de trazer a evidência para o Windows. Este roteiro não cria leases, não grava autenticação de API de nuvem e não executa a exclusão por você.
