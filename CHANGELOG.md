# Changelog

Formato [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/). A numeração do Hub
(`A.B.C`) está em [docs/VERSIONING.md](docs/VERSIONING.md); o filtro de imagem tem numeração
própria e vai indicado em cada entrada.

## [1.19.39] - 2026-11-15

Filtro de imagem 0.29. Perfis: Pepinho 1.2.0, Pepe 1.2.0, Capella 5.2.0. Ainda não validada
num pod; sem tag nem release.

### Adicionado

- Bancada Linux: link de convite para abrir o Open WebUI do pod.
- Troca de personagem no chat, com as correções vindas dos testes no pod.
- Visualizador: zoom ancorado no cursor, fundo translúcido, arrastar livre e autocomplete de `[`;
  visualizador das imagens enviadas.
- Pré-carga do modelo de chat.
- Costura com correção de tom nas edições com área; correção de tom em cada edição de uma
  fonte (o brilho fora da área volta ao da fonte, sem escurecer).
- Modo redesenho (padrão no Qwen) e escada do "nada mudou".
- Valves: balões preservados na troca e área por silhueta e recorte.
- Painel Prompt e receita por imagem; regerar com a mesma semente; ver sem colagem e refazer
  a imagem inteira.
- Escritores de prompt de visão e de texto cru; contrato de referências (mapa de slots com
  posição, número no chat, papéis e fins).
- Par de prompts E/C com "Prefiro esta" e saída inteira (filtro 0.26).
- Filtro 0.28: edições com duas ou três fontes geram uma primeira versão (V2) e uma revisão
  (V3/E) com as mesmas fontes ordenadas e a mesma semente; a falha de um escritor cai para o
  prompt do roteador.
- Trace de cada turno com semente, tempos e id dos jobs.
- Limpeza das pastas de saída do ComfyUI.
- Pod: `scripts/pod_session.py` e `scripts/pod_watch.py` (instalação, túnel, coleta e
  acompanhamento ao vivo).

### Alterado

- Filtro 0.29, a partir da análise do pod de 15/11:
  - troca de personagem: a variação 1 é a imagem inteira desenhada pelo Qwen, sem colagem;
    a variação 2 é a composição;
  - remoção de peça de roupa vai para a edição geral; a remoção para pessoas e objetos fica
    com o modelo específico;
  - mudança de forma, inclusive global e por palavras de proporção, segue a ordem de métodos
    (Qwen);
  - na troca, a personagem nova leva a própria roupa; "capa" conta como roupa, não como fundo;
  - um formato dito só pelo roteador não muda o quadro da fonte numa edição.
- Pedido que muda a imagem toda segue direto; reconciliação da ação do roteador; resposta curta
  a uma pergunta pendente.
- Contexto do chat honesto sobre o que existe de imagem; composição da troca em resolução
  reduzida; canvas mínimo de 1 MP no restage; Qwen sem faixa preta; recortes condicionais por
  área.
- Máscaras de balão sem detecção; área pintada ignora notas.
- Perfil Capella 5.2.0.

### Corrigido

- Esc não desliga mais os botões.
- Fim do loop de recusas; guarda contra a promessa de imagem sem imagem; resposta falsa de
  imagem trocada durante o texto.
- Texto entre colchetes é ignorado pelo roteador.
- O filtro só lê arquivos do dono da conversa.
- Filtro 0.29.1: a edição feita pelo próprio Open WebUI (Pepe e Pepinho, sem roteador) volta ao
  brilho da imagem-fonte, como na Capella; o limite sem área sobe de 12 para 20 e o trace passa a
  registrar `tone_match` e `image_job`.
- Filtro 0.29.2: sem área, a correção de brilho mede a referência nas faixas laterais (15% de
  cada lado) em vez da imagem inteira, que inclui o que o pedido mudou; o limite sem área cai de
  20 para 16. Na cadeia de 4 edições do pod (Pepe e Pepinho) o brilho das laterais ficou a 0,0-0,7
  nível da fonte em cada edição. Perfis `pepe` e `pepe-8gb` passam a gerar em 1024x1024 (antes
  512x512, com salto de tamanho na primeira edição); 8 GB sem medição.
- Endurecimento: fora de um turno o filtro não lê nem grava arquivo por id (antes lia só com
  aviso); a busca de referências recusa endereços IPv6 que embutem um IPv4 privado (NAT64,
  6to4, `::/96`) e `fec0::/10`; o manifesto de artefatos passa a exigir `sha256`.

### Conhecido

- Instalações antigas do Pepe e do Pepinho continuam gerando em 512x512 até o tamanho ser
  mudado nas configurações do Open WebUI: o Open WebUI guarda o valor no banco, e o novo padrão
  1024x1024 só vale para instalações novas.
- O 1024x1024 do Pepinho não foi medido em 8 GB de VRAM.

### Versionamento

- A numeração passa a seguir `A.B.C` (ver `docs/VERSIONING.md`); o valor 1.19.39 foi confirmado
  pelo dono.
- Licença MIT, avisos de terceiros (`THIRD_PARTY_NOTICES.md`) e CI no GitHub Actions.
