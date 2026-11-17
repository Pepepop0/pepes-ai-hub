# Validação da entrega 1.12–1.15

Instale somente **1.15.0**. As etapas anteriores são snapshots cumulativos para histórico e comparação.

## Pacotes Windows

Cada snapshot foi compilado em worktree destacada e ambiente de build próprio. O ZIP foi extraído fora do checkout; os executáveis extraídos responderam `--version`, `--smoke` com `ok: true` e `hub commands`. A varredura dos arquivos do ZIP não encontrou as três credenciais conhecidas usadas na verificação de distribuição.

| Hub | Commit do pacote | Bytes do ZIP | SHA256, primeiros 12 caracteres |
| --- | --- | ---: | --- |
| 1.12.0 | `3f6ba0d` | 31599008 | `478929dd20c2` |
| 1.13.0 | `27c6e42` | 31607361 | `9df140a9dfeb` |
| 1.14.0 | `12f48db` | 31611906 | `dfd6c8ba545c` |
| 1.15.0 | `6e3f2f5` | 31619600 | `11aa17385d80` |

Arquivos em `dist/PepitosAIHub-SkyRig-v<VERSÃO>-win64.zip`, acompanhados de `.zip.sha256` e `release-evidence-v<VERSÃO>.json`. Os manifestos registram o SHA256 completo e os resultados observados do smoke.

## Verificação de código

- `ruff format assets src tests`: 277 arquivos sem mudanças.
- `ruff check assets src tests`: aprovado.
- Suíte completa: **3848 passed, 35 skipped**, em 636,61 s.
- Backend nativo em ambiente com dependências reais do Open WebUI: **38 passed**. Inclui autenticação, ownership, publicação, seleção, aprovação idempotente, preferências e instalador. O teste de chat temporário vazio foi ajustado para o contrato de leitura de defaults; escrita sem ownership continua recusada.
- `node --check assets/open-webui/pepito_image_ui.js`: aprovado após o acabamento final de foco.

## Superfície real, sem carregar modelos

Open WebUI **0.12.0** instalado em ambiente descartável; extensão aplicada pelo instalador de produção. Backend de texto controlado, três imagens sintéticas. Nenhum peso da Capella foi carregado.

Observado no Chromium:

- Galeria nativa, prompt diagnóstico ausente, bordas arredondadas e ações no menu.
- Viewer em tela cheia, zoom e navegação; Escape fecha e devolve foco à imagem após sincronizar a variação. Esse último percurso revelou e corrigiu uma perda de foco causada por desabilitar temporariamente o botão durante o carregamento.
- Aprovação registrou uma amostra; repetir a mesma aprovação não duplicou aprendizado.
- Canvas abriu, pintura gerou chip e não inseriu payload no texto.
- Com Imagem desligada, o envio preservou o texto exato e a área, exibindo instrução para ativar Imagem.
- Preferências salvaram `auto_region=false` e `comparison_seed=42`; leitura HTTP retornou os mesmos valores. Sem editores configurados nesse ambiente, o seletor não inventou opções.
- Reset confirmado zerou o ledger da conta; estado da imagem/conversa permaneceu acessível. Imagens anteriores ao reset continuam utilizáveis como origem; tentativas apagadas não podem voltar a ensinar por aprovação sem registro.
- Layout observado em 1280×900 e 390×844. O menu de preferências possui rolagem interna na largura pequena.

Os dois serviços de validação e a aba gerenciada foram encerrados após o smoke.

## Limites observados

Sem inferência no RTX 5090, esta entrega não comprova latência, adesão semântica, precisão do grounding ou qualidade estética dos modelos. A região automática usa caixas estimadas pelo modelo visual existente, não segmentação SAM. Incerteza pede máscara manual; não autoriza uma edição de quadro inteiro como fallback.

A composição de pixels protege o exterior da máscara, mas não recupera detalhes dentro de uma região redesenhada nem elimina a deriva de operações globais. Seed fixa torna o experimento controlado; não implica ruído equivalente entre arquiteturas diferentes. O roteamento e a memória estruturada reduzem dependência do transcript, sem tornar ilimitado o contexto do modelo nem desativar a compactação do Open WebUI.

Roteiro econômico: [runbook-skyrig-1.15.md](runbook-skyrig-1.15.md). Backlog implementado: [backlog-madrugada-concluido.md](backlog-madrugada-concluido.md).
