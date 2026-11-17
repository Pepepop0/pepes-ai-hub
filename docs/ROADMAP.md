# Planejamento do Hub desktop

Este plano organiza a superfície atual e as funções candidatas. A branch `desktop-ui-shadcn` entrega o redesenho desktop e integra o motor existente. As propostas abaixo não são funcionalidades entregues nem autorizações para alterar downloads, licenças ou serviços silenciosamente.

## Entrega desta branch

O escopo de implementação está no [PRD](PRD.md), com regras destrutivas no [PRD de instalações](PRD_INSTALLATIONS.md):

- Janela Windows com React, shadcn/ui e WebView2
- Navegação por tarefas: visão geral, instalações, catálogo, atividade e configurações
- Instalação guiada, compatibilidade, destino, espaço e revisão
- Iniciar, parar, reiniciar e abrir Open WebUI
- Cadastrar existente, localizar movida, alternar, abrir pasta, verificar e reparar
- Exclusão protegida e remoção de referências como ações distintas
- Catálogo de modelos e perfis baseado nos manifests reais
- Eventos e diagnóstico acessíveis sem terminal
- Autorização Hugging Face e tema pela interface
- Build desktop, PRDs, arquitetura, operação e checklist de validação

## Inventário de funções e lacunas

A prioridade indica dependências e risco, não um prazo prometido.

| Área | Função | Situação ou proposta | Dependências e critério de conclusão |
| --- | --- | --- | --- |
| Instalações | Abrir pasta raiz, modelos e logs | Escopo desktop | Abrir somente caminhos conhecidos e existentes |
| Instalações | Cadastrar e localizar pasta movida | Escopo desktop | Preservar registro antigo em falha; verificar após mover |
| Instalações | Excluir ou esquecer | Escopo desktop | Confirmações distintas e proteção do motor |
| Instalações | Verificar e reparar | Escopo desktop | Verificação sem mutação e reparo explícito |
| Instalações | Renomear rótulo de apresentação | Proposta P2 | Não renomear pasta nem perfil; schema de preferências |
| Instalações | Mover pelo próprio aplicativo | Proposta P1 | Cópia verificada, espaço suficiente, parada, recuperação após interrupção |
| Instalações | Backup e restaurar conversas/configuração | Proposta P1 | Consistência do banco, destinos explícitos, teste real de restauração |
| Instalações | Exportar pacote portátil | Proposta P2 | Tratar ambientes Python não relocáveis, licenças e segredos excluídos |
| Modelos | Catálogo e composição dos perfis | Escopo desktop | Não confundir disponível com instalado |
| Modelos | Biblioteca instalada e consumo real | Proposta P1 | Conciliar Ollama, arquivos, hashes e referências dos perfis |
| Modelos | Trocar e importar modelo de chat (Ollama/GGUF) | Entregue | Escolha por instalação, rollback em falha, remoção libera arquivos |
| Modelos | Importar modelos de imagem (checkpoint/LoRA) | Proposta P1 | Workflow por modelo, VRAM, licença e revisão do plano |
| Modelos | Favoritos e filtros salvos | Proposta P2 | IDs canônicos estáveis, preferências locais |
| Modelos | Cache compartilhado entre instalações | Proposta P2 | Contagem de referências, isolamento de exclusão e política de volume |
| Perfis | Adicionar YAML em `addons` | Proposta P1 | Schema estrito, precedência de IDs, validar paths e URLs, explicar origem |
| Perfis | Importar um pacote declarativo | Proposta P1 | Não executar código do pacote; hashes, licenças e revisão do plano |
| Perfis | Editor visual de perfil | Proposta P2 | Construir sobre importação validada; preservar chaves suportadas |
| Downloads | Progresso e falhas reais | Motor existente, UI integrada | Não inventar porcentagem quando o tamanho for desconhecido |
| Downloads | Pausar/cancelar com segurança | Proposta P1 | Protocolo no motor, estado retomável, testes de cancelamento por etapa |
| Downloads | Fila e limite de banda | Proposta P2 | Agendador único; conflito com instalação não deve duplicar arquivos |
| Runtime | Ciclo de vida e conflitos externos | Escopo desktop | Prova de propriedade de processos e decisões explícitas |
| Runtime | Orçamento de VRAM por perfil | Motor existente, validação física pendente para Capella | Medir troca chat/imagem, contexto e recuperação de falhas |
| Runtime | Iniciar com Windows ou minimizar na bandeja | Proposta P2 | Consentimento, regra de saída, instância única e autostart reversível |
| Runtime | Portas configuráveis pela UI | Proposta P2 | Atualizar consumidores e validar conflitos de forma transacional |
| Diagnóstico | Doctor e eventos da sessão | Escopo desktop | Mensagens úteis sem segredos |
| Diagnóstico | Exportar pacote de suporte sanitizado | Proposta P1 | Remover tokens, prompts e dados pessoais; revisão antes de exportar |
| Diagnóstico | Histórico completo persistente | Proposta P2 | Retenção e limites de disco; distinguir de eventos da sessão |
| Atualizações | Atualizar Open WebUI pelo Hub | Proposta P1 | Pin compatível, backup de dados e validação da versão instalada |
| Atualizações | Atualizar o Hub automaticamente | Proposta P2 | Releases assinadas, assinatura verificada, recuperação e canal estável |
| Distribuição | Executável desktop e CLI de diagnóstico | Escopo desktop | Bundle completo e smoke fora do checkout |
| Distribuição | Assinatura de código e instalador | Proposta P1 | Certificado e pipeline de releases; não prometer ausência de SmartScreen |
| Integração | Chat e imagens via Open WebUI | Preservado | Não construir um segundo cliente de chat no Hub |
| Integração | Tool calling validado por perfil | Proposta P1 | Cenários reproduzíveis e compatibilidade modelo/versão |
| Imagens | Troca de personagem: Qwen-Edit sem o redimensionamento interno para 1 MP | Débito técnico P3 | O G2 já compensa o deslocamento na composição; corrigir na geração (referência e latente na mesma grade, como o LockPixel) só limpa o encaixe. Não resolve Q3/Q4, que são conteúdo do modelo (`docs/testing/swap-lab.md`) |
| Integração | OAuth Hugging Face | Requer client ID registrado | Consentimento, erro/expiração e revogação testados |
| Qualidade | GPU física, disco quase cheio, reinício | Validação contínua | Evidência por hardware; não inferir de mocks |
| Acessibilidade | Teclado, foco, contraste e janela estreita | Escopo desktop | Conferir todos os diálogos e temas na interface real |
| Plataforma | Linux/macOS, AMD e multi-GPU | Fora do escopo atual | Revisar detecção, processos, provisioners, WebView e empacotamento |

## Ordem recomendada depois do redesenho

Primeiro valide a aplicação na máquina alvo; depois amplie a superfície de gerenciamento:

1. Executar o checklist Capella no SkyRig e registrar modelos, drivers, memória e resultado
2. Adicionar backup/restauração antes de oferecer atualizações potencialmente destrutivas
3. Implementar inventário real de modelos e dependências por perfil
4. Introduzir `addons` declarativos com validação e revisão antes da instalação
5. Implementar cancelamento seguro no motor antes de desenhar botões de pausa
6. Entregar atualização controlada do Open WebUI com backup e verificação de versão
7. Assinar releases antes de automatizar a atualização do próprio Hub

## Decisões para sua avaliação

Estas opções simplificam o desenvolvimento sem remover proteções:

| Recomendação | Benefício | Custo ou limite |
| --- | --- | --- |
| Manter um único motor Python | Reaproveita provisioners, testes e regras de processo | Empacotar Python e WebView2 corretamente |
| Usar WebView2 em vez de Electron | Não distribuir outro Chromium | Windows precisa do runtime WebView2 |
| Manter chat no Open WebUI | Evita duplicar conversas, upload e geração | Experiência de chat continua em outro aplicativo/navegador |
| Adotar addons somente declarativos | Adicionar modelos sem plugins executáveis | Não suporta qualquer código de terceiros |
| Uma operação de mutação por vez | Reduz corrupção, disputa de portas e conflitos de pasta | Não faz instalações simultâneas |
| Separar catálogo de inventário | Evita sugerir que um modelo já está instalado | Exige uma etapa própria para descoberta e hashes |
| Adiar auto-update até assinatura e backup | Evita comprometer uma instalação funcional | Atualização manual durante a pré-versão |

## Regras para aceitar funções novas

Cada proposta precisa de uma jornada, uma alteração de domínio identificada, proteção contra interrupção e evidência de funcionamento. Botões sem implementação, dados demonstrativos no produto e sucessos simulados não contam como entrega.

Novas funções entram primeiro no plano; não devem aparecer habilitadas na interface antes de terem comportamento completo. Os critérios verificáveis ficam junto do PRD correspondente.
