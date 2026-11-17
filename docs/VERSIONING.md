# Versionamento do Hub

Formato: **`A.B.C`**, por exemplo `1.19.27`. Definido pelo dono do projeto em 15/11/2026.

| Parte | O que é | Quem decide |
|---|---|---|
| **A** (`1`) | Versão geral da release. | O dono, quando decide mudar. |
| **B** (`19`) | Versão mais recente do Hub: mudanças maiores e de interface do Hub. | Sobe com mudanças maiores. |
| **C** (`27`) | Versão de fix: cada alteração ou atualização menor que entra durante os testes de fluxo. | Sobe a cada entrega nova validada em teste de fluxo. |

## O que conta e o que não conta no C

- **Conta:** alteração ou atualização menor entregue e testada num teste de fluxo (correção, ajuste de interface, comportamento novo pequeno).
- **Não conta:** labs (experimentos no pod que medem opções e não entregam produto), pesquisa, documentação e specs que não mudam o que o usuário recebe.

## Onde o número vive

- `pyproject.toml` (`version`), `src/local_ai_stack/__init__.py` (`__version__`) e `desktop-ui/package.json` (`version`): o mesmo valor nos três.
- Nome dos artefatos: `PepitosAIHub-Capella-v<A.B.C>-linux-amd64.tar.gz` e `PepitosAIHub-SkyRig-v<A.B.C>-win64.zip`.
- O **filtro de imagem** (`assets/open-webui/pepito_image_chat.py`, cabeçalho `version:`) tem numeração própria (`0.29` em 15/11) e não é o número do Hub; o PLANO registra qual filtro vai em cada versão do Hub.

## Histórico observado (pyproject.toml, de `git log`)

`1.10.0`, `1.11.0`, `1.11.1`, `1.12.0`, `1.13.0`, `1.14.0`, `1.15.0`, `1.16.0`, `1.17.0`, `1.17.2`, `1.18.0`, `1.19.0` (04/11/2026). Até aqui o terceiro número quase sempre ficou em `0`; `1.11.1` e `1.17.2` foram os únicos fixes numerados. A regra acima vale daqui para a frente.

## Regras de trabalho

- Sem push, tag ou release sem pedido do dono. Os commits ficam locais, nos dois ramos (`test/all-in-one` e `desktop-ui-shadcn`), com a diferença entre eles conferida por hash.
- O número do C só muda quando o dono confirma o valor. Ao subir, atualizar os três arquivos, o PLANO (seção "Estado atual") e gerar TAR e ZIP com o novo nome.
- **Valor atual: `1.19.39`, confirmado pelo dono** (filtro `0.29`). `pyproject.toml`, `__init__.py` e `desktop-ui/package.json` carregam esse valor.

## Recontagem do C desde a 1.19.0 (15/11/2026)

O repositório não tem um contador do C; a recontagem sai do histórico (`git log d2f0b74..test/all-in-one`, a 1.19.0 de 04/11). O resultado depende do critério:

| Critério | Resultado |
|---|---|
| Commits desde a 1.19.0 (sem merges) | 105 |
| Commits que tocam produto (filtro, JS/CSS, nativo, `src/`, workflows, perfil, `desktop-ui/`) | 53 |
| Subidas da versão do filtro (0.20 → 0.27) | 7 |
| **Entregas distintas de produto** (commits do mesmo assunto agrupados; labs, pesquisa, docs e specs sem efeito no produto não contam) | **39** |

**27 não sai de nenhuma contagem do histórico.** Coincide só com o filtro `0.27`.

As 39 entregas, em ordem:

1. Linux bench: link de convite · 2. Troca de personagem no chat · 3. Correções da troca vindas do pod · 4. Zoom do visualizador e fundo translúcido · 5. Esc não desliga os botões · 6. Pré-carga do modelo de chat · 7. Costura com correção de tom nas edições com área · 8. Filtro só lê arquivos do dono · 9. Contexto honesto do chat · 10. Pedido que muda a imagem toda segue direto · 11. Texto entre colchetes ignorado · 12. Reconciliação da ação do roteador · 13. Guarda contra promessa de imagem · 14. Resposta curta a pergunta pendente · 15. Trace com semente, tempos e id dos jobs · 16. Zoom ancorado no cursor · 17. Limpeza das pastas do ComfyUI · 18. Balões preservados na troca (Valve) · 19. Área por silhueta e recorte (Valve) · 20. Modo redesenho · 21. Escada do "nada mudou" · 22. Composição da troca em resolução reduzida · 23. Área pintada ignora notas · 24. Máscaras de balão sem detecção · 25. Canvas mínimo de 1 MP no restage · 26. Visualizador das enviadas, arrastar livre, autocomplete de `[` · 27. Fim do loop de recusas · 28. Krea primeiro na troca · 29. Painel Prompt e receita por imagem (SPEC-308) · 30. Redesenho padrão no Qwen · 31. Resposta falsa de imagem trocada durante o texto · 32. Qwen sem faixa preta · 33. Recortes condicionais por área · 34. Escritores de prompt visão/cru · 35. Padrões da Onda 3 integrados · 36. Regerar com a mesma semente (317) · 37. Ver sem colagem e refazer inteira (318) · 38. Par E/C, "Prefiro esta" e saída inteira (319) · 39. Contrato de referências (320).

Com essa contagem o C é **39**, valor confirmado pelo dono. Variações razoáveis do critério davam de 37 a 40 (por exemplo, tratar o item 15 como observabilidade, que não muda o que o usuário vê, ou a integração do item 35 como parte dos itens anteriores).

## Proposta (boas práticas de mercado), a decidir

1. **Forma SemVer `MAJOR.MINOR.PATCH`**, que é a forma do `A.B.C`. Atenção: no SemVer, funcionalidade nova pequena sobe o MINOR; aqui o C inclui "atualização menor", então é uma convenção própria do projeto. Registrá-la como tal (este documento) evita confusão.
2. **Versão do filtro separada**, como o `appVersion` do Helm ou o tag de imagem de um componente: o Hub tem o seu número, o filtro tem o dele, e uma tabela no PLANO diz qual filtro vai em cada Hub. É o que evita a coincidência `1.19.27` × `0.27`.
3. **Metadados de build do SemVer (§10)** para ligar os dois sem confundir a precedência: `1.19.39+filter.0.27` (no `--version` e no nome do artefato, se o `+` for aceito pelo empacotador; senão `1.19.39-f0.27` não, pois seria pré-release).
4. **Pré-release enquanto aguarda validação em pod**: `1.19.39-rc.1`; vira `1.19.39` quando o teste de fluxo aprova. Labs não geram versão.
5. **Fonte única do número**: hoje são três arquivos; gerar os outros a partir de um (ou um teste que falha se divergirem).
6. **CHANGELOG** no formato Keep a Changelog, uma linha por C, com data, o que mudou e o filtro. A recontagem acima viraria a primeira versão do arquivo.
7. **Automação**: um `scripts/bump_version.py` que atualiza os três arquivos, o PLANO e o CHANGELOG. Commits no padrão Conventional Commits permitiriam gerar o CHANGELOG e o bump automaticamente (release-please ou python-semantic-release), mas isso muda o hábito de commit e só compensa se o projeto for publicado.
8. **Tags anotadas** (`v1.19.39`) só quando o dono pedir; hoje nenhum tag é criado.
