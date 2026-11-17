# Compilar o aplicativo desktop para Windows

Este guia gera uma pasta distribuível com a janela desktop e um executável separado para diagnóstico. O computador de destino não precisa de Node.js ou Python instalado para abrir o Hub; precisa do runtime Microsoft Edge WebView2. Para criar uma instalação, também precisa do Git for Windows: se estiver ausente, o assistente oferece instalação pelo `winget` após confirmação explícita ou abre o instalador oficial; reabra o Hub depois.

## Preparar o ambiente de desenvolvimento

Use Windows 10/11 x64, Python 3.11 e Node.js 22.12 ou mais recente. O bootstrap instala as dependências Python, as dependências do renderer pelo lockfile e compila React.

Na raiz do checkout, execute:

```powershell
.\bootstrap-dev.bat
.\run.bat
```

O frontend fica em `desktop-ui/`. O comando de produção é `npm run build`; ele verifica TypeScript e gera `desktop-ui/dist`. Node.js não acompanha o aplicativo distribuído.

## Gerar o bundle

O script usa a `.venv` do checkout e o PyInstaller fixado em `pyproject.toml`:

```powershell
.\scripts\build_windows.ps1
```

O processo:

1. Instala as dependências Python de build e desktop
2. Executa `npm ci` e `npm run build` no frontend
3. Remove somente as saídas anteriores pertencentes a este build
4. Empacota Python, pywebview, bibliotecas, manifests e renderer
5. Confere os executáveis e arquivos essenciais
6. Executa `--smoke` e `--version` pelo executável de diagnóstico

Use `-SkipSmoke` apenas quando precisar omitir a verificação final. `-Version` imprime a versão sem compilar.

## Distribuir a pasta completa

A saída contém dois executáveis que usam os mesmos arquivos internos:

```text
dist/PepitosAIHub/
  PepitosAIHub.exe
  PepitosAIHub-CLI.exe
  _internal/
    desktop-ui/dist/
    profiles/
    manifests/
    workflows/
    assets/
```

Abra `PepitosAIHub.exe` para usar a janela nativa sem console. Não copie apenas o executável; `_internal` contém o renderer e as dependências. O tamanho final depende das bibliotecas empacotadas e deve ser medido no build, não inferido de versões anteriores.

## Executar diagnósticos

Use o CLI quando precisar de saída textual ou entrada pelo terminal:

```powershell
.\PepitosAIHub-CLI.exe --smoke
.\PepitosAIHub-CLI.exe --doctor
.\PepitosAIHub-CLI.exe --hf-status
```

`--hf-login` e `--hf-logout` continuam disponíveis no CLI. Na janela desktop, use **Configurações → Hugging Face** para essa credencial, **Configurações → Civitai** para os modelos Capella hospedados lá e **Configurações → Pesquisa web e referências** para ligar/desligar a pesquisa, informar um SearXNG e cadastrar chaves Brave/Unsplash/Pexels opcionais. As chaves são validadas antes de salvar e protegidas pelo DPAPI do usuário atual; os valores nunca voltam para a interface. Reinicie os serviços depois de alterar Unsplash/Pexels. Não cole tokens em issues, capturas de tela ou logs.

Agentes de código operam o Hub sem janela pelo mesmo CLI: `hub state`, `hub commands`, `hub run <comando> [--args JSON] [--wait]` e `hub serve --no-open`, com saída JSON. Enquanto a janela está aberta, apenas comandos de leitura funcionam; os demais saem com `desktop_running`. Veja o [guia para agentes](docs/AGENT_WORKFLOW.md).

## Executar a interface no navegador para desenvolvimento

O mesmo servidor e os mesmos assets podem abrir no navegador, sem simular o backend:

```powershell
.\.venv\Scripts\python.exe -m local_ai_stack.desktop --browser
.\.venv\Scripts\python.exe -m local_ai_stack.desktop --browser --no-open
```

`--no-open` só imprime a URL, sem abrir o navegador; é o modo para automação. `PepitosAIHub-CLI.exe hub serve [--no-open]` faz o mesmo no bundle. Só uma instância do Hub (janela ou navegador) abre por vez; a segunda informa que o Hub já está aberto e encerra.

O servidor escuta somente em `127.0.0.1`, em uma porta escolhida pelo sistema. A URL de desenvolvimento inclui uma credencial temporária no fragmento; não a publique. Essa opção é para depuração local, não para disponibilizar o Hub na rede.

## Recursos e dados de usuário

`local_ai_stack.resources.resource_root()` resolve os arquivos embarcados em `_internal` ou no checkout. `PEPITOS_AI_HUB_RESOURCES` permite usar outra raiz de recursos durante desenvolvimento.

As instalações e o registro global mantêm seus caminhos anteriores. Uma instalação portátil usa `stack/` ao lado do executável, nunca dentro de `_internal`. Credenciais protegidas por DPAPI pertencem ao usuário e computador Windows; copiar a pasta da aplicação não transfere os logins. Configurações sem segredo, como ligar a pesquisa e o endereço do SearXNG, ficam no diretório de configuração do usuário.

O Hub provisiona seu Python gerenciado para Open WebUI e ComfyUI quando necessário. O Python embutido no executável não deve ser usado para criar esses ambientes.

## WebView2 e falhas de abertura

Se a janela não abrir, confira o [runtime WebView2 da Microsoft](https://developer.microsoft.com/microsoft-edge/webview2/). O Hub usa esse runtime em vez de distribuir outro Chromium.

Se o smoke apontar ausência de `desktop-ui/dist/index.html`, compile o frontend antes de empacotar. Se um bundle movido falhar, confira se a pasta `_internal` foi copiada integralmente.

## Limites de distribuição

O build permanece sem assinatura de código. SmartScreen pode exibir um aviso; o redesign não adiciona certificado, instalador MSI ou atualização automática.

O teste de imports e recursos não prova que WebView2 abriu nem que modelos rodaram na GPU. Execute o [checklist desktop](docs/testing/desktop-ui-checklist.md) e, para Capella, o [checklist RTX 5090](docs/testing/capella-5090-checklist.md).
