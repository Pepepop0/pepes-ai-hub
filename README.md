# Pepe's AI Hub

Hub local para instalar e gerenciar uma stack de IA local: Ollama, Open WebUI e ComfyUI.

## O que é

Uma aplicação para Windows que cria, opera e mantém instalações locais de IA a partir de uma interface única.

- instala e gerencia **Ollama**, **Open WebUI** e **ComfyUI**
- configura cada instalação a partir de um perfil pronto
- permite ter mais de uma instalação e alternar entre elas
- interface em **PT-BR**

## Recursos atuais

- criação e gerenciamento de instalações
- troca entre instalações
- exclusão / desinstalação de instalações
- perfis prontos para diferentes níveis de hardware
- iniciar, parar e reiniciar a stack
- abrir o Open WebUI pelo Hub
- verificação e reparo da instalação
- chat local
- geração de imagem
- edição de imagem

## Screenshots

### Dashboard

Instalação atual, estado dos serviços e ações principais.

![Dashboard](assets/screenshots/dashboard.png)

### Instalações

Instalações conhecidas, troca de instalação e manutenção.

![Instalações](assets/screenshots/installations.png)

## Perfis disponíveis

| Perfil | Faixa | Validação |
| --- | --- | --- |
| **Pepinho** | 8 GB | orçamento de 8 GB validado; ainda não testado em GPU física de 8 GB |
| **Pepe** | 12 GB | validado em RTX 5070 real |
| **Capella** | 32 GB | pronto no código; validação em RTX 5090 física pendente |

Os recursos disponíveis (chat, geração e edição de imagem) e os modelos usados dependem do perfil.

### Capella e Hugging Face

Capella usa o FLUX.2 Klein 9B FP8, um repositório *gated* (licença não comercial). Antes de instalar:

1. com a sua conta do Hugging Face, aceite a licença em <https://huggingface.co/black-forest-labs/FLUX.2-klein-9b-fp8>;
2. rode `local-ai-stack --hf-login` (ou `PepitosAIHub.exe --hf-login`) e cole um token *read* de <https://huggingface.co/settings/tokens>. O token é validado, guardado criptografado pelo Windows (DPAPI) e enviado só para `https://huggingface.co`;
3. `--hf-status` confirma identidade e acesso ao modelo; `--hf-logout` apaga a credencial. `HF_TOKEN` no ambiente tem precedência sobre a credencial salva.

Na RTX 5090 o chat (27B) e o FLUX não ficam residentes juntos: cada turno de imagem descarrega o Ollama antes do ComfyUI e libera o ComfyUI antes da resposta do chat.

## Coming next

Épicos e frentes do backlog, sem data definida:

- nova UI / UX
- expansão da biblioteca de modelos
- melhorias de tool calling
- atualização do Open WebUI pela interface do Hub
- atualização automática do Hub
- melhor empacotamento da aplicação
- assinatura de código
- validação em mais configurações de hardware
- melhorias de qualidade de vida baseadas em feedback

## Status

**v0.1.0-rc1** — pré-versão destinada a testes. Download na página de [Releases](https://github.com/Pepepop0/pepes-ai-hub/releases).
