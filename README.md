# Graph Generator

Grafo de citações para explorar uma área de pesquisa e escolher tema. Você cola um
artigo seminal; o programa segue **para frente no tempo** — quem citou o seminal, quem
citou esses — e monta uma página navegável onde cada nó abre autores, links, resumo e
tópicos, e onde você pode marcar o que interessa.

## Rodando

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"     # ou: pip install fastapi uvicorn httpx pydantic
set OPENALEX_MAILTO=seu.email@exemplo.org                # entra no "polite pool" do OpenAlex
.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

Abra <http://localhost:8000>. O grafo é gravado em `graph.db` ao lado do código:
fechar o servidor e voltar depois retoma exatamente o que estava na tela.

Variáveis de ambiente: `OPENALEX_MAILTO` (recomendado — sem ele o OpenAlex aplica
limites menores) e `GRAPH_DB` (caminho do banco, para manter grafos separados por
investigação).

## Por que não Google Scholar

O Scholar não tem API pública e bloqueia scraping com CAPTCHA depois de poucas
requisições — um crawler em cascata trava em minutos. A fonte é o
[OpenAlex](https://openalex.org) (sem chave de API, ~250M trabalhos, grafo de citações
completo), com o [Semantic Scholar](https://www.semanticscholar.org/product/api) como
apoio quando falta o resumo.

## Por que o grafo não é "completo"

Não pode ser. Um seminal com 10 mil citações gera milhões de nós em profundidade 2.
Em vez disso, cada nó traz os **top-N citantes mais citados** — o que corta o ruído e
traz o cânone da área — e o painel sempre diz a verdade sobre a cobertura
("mostrando 20 de 83.423 citantes"). Quando um assunto te interessa, você clica no nó
e **escava mais um nível** ali. O grafo cresce onde você tem interesse, não em toda
direção ao mesmo tempo.

Parâmetros do crawl inicial: profundidade, top-N por nó, ano mínimo e um teto de nós.
Se o teto interromper a travessia, a barra de status avisa — um grafo cortado nunca é
apresentado como se estivesse completo.

## Investigações

Cada busca é uma **investigação**: um seminal, seus parâmetros e o recorte de artigos
que desceu dele. Isso existe porque o método acima pede comparar recortes — o mesmo
seminal com e sem filtro de ano, ou dois seminais de subáreas diferentes.

O que é fato fica compartilhado, o que é recorte fica separado:

- `papers` e `edges` são **globais**. "Este artigo tem este título" e "X citou Y" não
  dependem da pergunta, e o cache global é o que torna instantâneo alternar entre
  investigações ou reaproveitar um artigo que já custou requisição.
- `members` (quem pertence a quê, e a que profundidade) e `expansions` (o que já foi
  escavado) são **por investigação**.
- `marks` é **global** de propósito: "interessa" quer dizer "interessa para minha
  tese", não "interessa para esta consulta". Reencontrar um artigo já marcado ao abrir
  outra investigação é justamente o sinal de sobreposição entre as duas.

**Migração automática.** Um `graph.db` de antes das investigações é convertido ao
abrir, sem perder nada nem baixar nada de novo. Se ele acumulava várias buscas no
mesmo grafo — o caso comum, e o incômodo que motivou isto —, as arestas dizem qual
artigo desceu de qual seminal, então as buscas são reconstruídas: uma investigação por
seminal, com as profundidades recalculadas. Um artigo alcançável pelos dois cones
aparece nos dois.

## As quatro visões

| Visão | Para quê |
|---|---|
| **linha do tempo (tópico × ano)** | Cada tema é uma raia, cada ano uma coluna. Uma raia que seca depois de 2019 é um assunto que morreu — a raia vem marcada em itálico com "parou em ⟨ano⟩". É a visão que responde "como os assuntos estão progredindo". |
| **radial por nível** | Seminal no centro, quem o citou no primeiro anel, e assim por diante. Mostra a estrutura de propagação. |
| **orgânico** | Clusters temáticos. As posições são semeadas por grupo antes de relaxar, porque a topologia sozinha não separa os temas: quase toda citação aponta para o seminal ou para um punhado de hubs. |
| **camadas** | Um nível por faixa, seminal no topo. Hierarquia limpa. |

## Como ler o desenho

- **tamanho** = número de citações (raiz quadrada, para 10.000 não virar um nó gigante)
- **forma** = grupo temático, *sempre*, em qualquer modo de cor
- **cor** = grupo, ou heatmap de citações (escala log), ou ano — você escolhe
- **borda tracejada** = nó nunca expandido; ainda há o que escavar ali
- **contorno dourado / verde / esmaecido** = interessa / lido / descartado
- **losango** = o artigo seminal
- seta aponta **do citante para o citado**, ou seja, para trás no tempo

A forma carrega o grupo mesmo quando a cor está dizendo outra coisa. Isso não é
decoração: a paleta categórica foi validada contra o fundo do canvas em modo
*all-pairs* (num grafo de nós, dois grupos quaisquer podem ficar lado a lado) e sua
separação para daltonismo cai na faixa que só é admissível com uma segunda
codificação. A forma é essa codificação — a identidade nunca depende só da cor.

**Granularidade do agrupamento.** O OpenAlex classifica cada artigo em três níveis, e
qual serve depende da pergunta. Medido num grafo real de 220 artigos partindo de
"Deep learning" (Nature, 2015):

| agrupar por | valores distintos | serve para |
|---|---|---|
| campo | 10 | resumir um grafo interdisciplinar |
| subcampo | 27 | separar vertentes dentro de uma área *(padrão)* |
| tópico | 68 | o detalhe fino |

Os 7 slots da paleta vão para os grupos mais frequentes, sempre na mesma ordem, mais o
grupo do próprio seminal, que nunca cai no cinza. O resto vira "outros".

## Limites conhecidos

- **Resumos faltam às vezes.** O OpenAlex não tem `abstract` para todo registro
  (~2/3 de cobertura na amostra medida). Quando falta e há DOI, buscamos no Semantic
  Scholar no momento em que você abre o nó — e o resultado fica gravado. Se os dois
  falharem, o painel diz isso em vez de inventar. Deliberadamente **não** buscamos por
  título: o S2 casaria com artigos homônimos e colaria o resumo errado no nó, o que é
  pior que resumo nenhum na hora de decidir se o artigo importa.
- **Metadados do OpenAlex às vezes erram.** "Attention Is All You Need" aparece com
  ano 2025 em alguns registros. É dado da fonte, não conserto nosso — e a escala de
  cor por ano herda o erro.
- **Preprints do arXiv** só resolvem quando estão indexados sob o DOI `10.48550/arXiv.*`.
  Quando não estão, o programa pede o título em vez de adivinhar.
- **Link ambíguo** (título, URL desconhecida) devolve os 5 melhores candidatos para
  você escolher. Nunca escolhe por você.

## Testes

```bash
.venv\Scripts\python.exe -m pytest              # unidade + rotas, tudo com fixtures
.venv\Scripts\python.exe -m pytest -m network    # bate na API real; pega quebra de contrato
```

A suíte padrão não toca a rede. Os testes marcados `network` existem para avisar
quando o OpenAlex ou o Semantic Scholar mudarem de comportamento — foi assim que
descobrimos a cobertura parcial de resumos e a descontinuação de `cited_by_api_url`.

## Estrutura

```
app/openalex.py   cliente, resolução do input, busca de citantes, inversão do abstract
app/s2.py         fallback de resumo (tolerante: qualquer falha devolve vazio)
app/crawl.py      BFS com orçamento; expansão de um nó
app/db.py         SQLite: papers, edges, expansions, marks
app/main.py       rotas + SSE de progresso
static/graph.js   canvas, escalas de cor, filtros, os quatro layouts
static/panel.js   painel de detalhes, triagem, botão de expandir
```

## Fora de escopo (por ora)

Exportar GraphML/CSV/BibTeX; percorrer para trás (referências em vez de citações);
grafo de co-autoria; clustering automático de comunidades; vários seminais no mesmo
grafo. Nenhum deles exige mudar o schema.
