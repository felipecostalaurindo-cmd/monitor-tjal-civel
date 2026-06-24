# Monitor TJ/AL Câmaras Cíveis — engine + drill-down (instruções para o Claude)

Este repositório é o **engine do Monitor das Câmaras Cíveis do TJ/AL** (1ª, 2ª, 3ª e 4ª Câmara
Cível + Seção Especializada Cível) **mais os snapshots semanais** de cada rodada. Toda semana o
monitor coleta os acórdãos na Consulta de Jurisprudência do 2º Grau (e-SAJ/cjsg do TJAL), classifica
por **matéria de direito material** (área/tema) + **classe** + **subtemas** + **incidentes
processuais**, posta uma tabela de percentuais por câmara no canal Slack `#tjal-camaras-civeis` e
arquiva a rodada em `registros/<AAAA-MM-DD>/`.

## Para que você (Claude) é chamado aqui

Você é acionado por **`@Claude` no canal `#tjal-camaras-civeis` do Slack** (Claude Code na nuvem).
A mensagem semanal mostra só os **percentuais** — sem números de processo. O usuário (Felipe,
advogado) responde pedindo o **drill-down**: os **números dos processos** (e, sob demanda, a
**ementa**) de um recorte. Seu trabalho é ler o snapshot da semana neste repo e devolver esses
acórdãos.

## ⚠️ ATENÇÃO AO VOLUME — a diferença crítica deste monitor

O cível é **grande**: uma rodada tem **ordem de milhares** de acórdãos (a de 2026-06-21 tem 2.205).
Um recorte largo **não cabe** numa mensagem do Slack, e o bot **não entrega respostas longas** (elas
ficam só na sessão e, para o usuário, parece que você não respondeu). Por isso:

**REGRA DE OURO — comece pela contagem, estreite, e só então liste números (sem ementa).**

1. **Nunca despeje uma lista grande.** Se o recorte tiver **mais de ~100 acórdãos**, NÃO liste os
   números. Em vez disso, responda com o **total** e a **distribuição** (por câmara e por classe) e
   **peça para estreitar**. Ex.: "Bancário e Financeiro = 554 acórdãos (4ª: 190, 3ª: 170, 1ª: 110,
   2ª: 84). É muito pra listar de uma vez — quer por câmara? por classe (Apelação/Agravo)? por
   subtema?".
2. **Liste números só quando o recorte for pequeno** (≲ ~100 acórdãos): uma linha por acórdão com
   **número CNJ + câmara + relator** — **SEM ementa**. Abra dizendo o total e a semana.
3. **Ementa só sob demanda, de processos específicos** ("me dá a ementa do 0743532-12"). Nunca traga
   várias ementas de uma vez (estoura o tamanho).
4. Se mesmo estreitado o recorte passar de ~100, ou de ~3.500 caracteres, **poste em blocos**
   (várias respostas no thread) avisando quantos faltam, **ou** sugira pegar a lista completa fora do
   Slack (rodar o `filtrar --formato csv` localmente / abrir o `classificado.csv` deste repo).

## Onde estão os dados

Cada rodada vive em **`registros/<AAAA-MM-DD>/`** (rótulo = fim da janela). Arquivos:

- **`classificado.csv`** — a **fonte do drill-down**. Uma linha por acórdão, com: `numero` (CNJ),
  `orgao` (câmara), `classe`/`classe_curta`, `area` (matéria dominante), `tema`, `subtema`,
  `assunto`, `incidentes`, `relator`, `comarca`, `data_julgamento`/`data_registro`/`data_publicacao`,
  `ementa` (texto completo, e-SAJ) e `url_pdf` (link do inteiro teor). **A `ementa` é o texto que
  você mostra quando pedirem; o `url_pdf` é o PDF do acórdão.**
- `resumo.json` — agregados (percentuais de tema por câmara, classes, incidentes, tendência Δ p.p.).
- `resumo.md` — versão legível. `slack.txt` — a mensagem postada.

**Qual rodada usar:** a **mais recente** em `registros/` (maior data), salvo se o usuário citar outra.

## Como fazer o drill-down (NÃO recolete do e-SAJ)

Os dados já estão arquivados — **não rode `coletar`** para um drill-down (é lento e pode divergir do
que foi postado). Filtre o snapshot com `filtrar`:

```bash
# 1) SEMPRE comece medindo o tamanho do recorte (conte antes de listar):
python3 monitor_tjal.py filtrar --inp registros/<AAAA-MM-DD>/classificado.csv \
  --area "Bancário" --formato csv | tail -n +2 | wc -l

# 2) PADRÃO (enxuto — número + câmara + relator, SEM ementa): só se o recorte for pequeno
python3 monitor_tjal.py filtrar --inp registros/<AAAA-MM-DD>/classificado.csv \
  --area "Saúde" --camara "4ª" --classe "Apela"

# 3) SOB DEMANDA (ementa — só quando pedirem, de processos específicos):
python3 monitor_tjal.py filtrar --inp registros/<AAAA-MM-DD>/classificado.csv \
  --texto "0743532-12" --com-ementa
```

Filtros (todos **substring, case-insensitive**, combináveis — combine para estreitar!):

| Flag | Filtra por | Exemplos |
|---|---|---|
| `--area` | matéria/área | `"Bancário"`, `"Responsabilidade"`, `"Saúde"`, `"Consumidor"` |
| `--tema` | tema/assunto | `"contrato"`, `"dano moral"`, `"honorários"` |
| `--classe` | classe processual | `"Apelação"`, `"Agravo de Instrumento"`, `"Embargos"` |
| `--camara` | câmara | `"1ª"`, `"2ª"`, `"3ª"`, `"4ª"` |
| `--subtema` | subtema lido na ementa | `"Consignado"`, `"Tarifa"` |
| `--incidente` | incidente processual | `"Tutela"`, `"Honorários"`, `"Gratuidade"` |
| `--relator` | relator(a) | `"Tenório"` |
| `--texto` | termo na ementa (e CNJ p/ achar 1 processo) | `"usucapião"`, `"0743532-12"` |
| `--com-ementa` | **inclui a ementa** na saída (use só quando pedirem) | — |
| `--max-ementa N` | corta a ementa em N caracteres (default 1200) | — |

`--formato csv` para contar/tabular. Sem `--com-ementa` a saída traz número + classe + tema +
incidentes + câmara + relator + data + PDF.

### Áreas (`--area`) — mapeie o pedido para uma destas

`Bancário e Financeiro` · `Contratos` · `Responsabilidade Civil` · `Saúde` · `Processual Civil` ·
`Consumidor` · `Servidor Público e Administrativo` · `Tributário` · `Usucapião e Direitos Reais` ·
`Locação e Imóveis` · `Sucessões` · `Empresarial` · `Família` · `Previdenciário` · `Plano de Saúde` ·
`Ambiental` · `Registros Públicos`. (Distribuição exata da semana no `resumo.json`.) Aqui a **área é
dominante** (uma por acórdão, soma ~100%); os **incidentes** é que são multivalorados.

## Como responder no Slack

- **Lidere pela contagem.** Diga o **total** do recorte e de qual **semana**. Se for grande, mostre a
  distribuição (por câmara/classe) e ofereça estreitar — não liste tudo.
- Quando listar: **número CNJ + câmara + relator** (uma linha por acórdão), escaneável.
- Os acórdãos são **públicos** — pode listar número e link à vontade. Link = o `url_pdf` da linha
  (`https://www2.tjal.jus.br/cjsg/getArquivo.do?cdAcordao=<cd>&cdForo=<foro>`).
- Ementa: só sob demanda, de processos específicos.

## Limitações honestas (não esconda do usuário)

- **Volume:** recortes largos não cabem no Slack — ver a regra de ouro acima. Seja transparente
  quando precisar quebrar em blocos ou mandar pegar a lista fora do canal.
- **Classificação determinística:** área/tema/classe/incidentes vêm de mapa de assunto CNJ + léxico
  sobre a ementa. Alta precisão, mas não infalível — se algo parecer fora do recorte, confira a
  ementa/PDF antes de afirmar.
- **Nunca cite um acórdão que não esteja no `classificado.csv` da rodada.** Não traga julgados de
  fora do snapshot.

## Coleta nova (só se pedirem explicitamente uma janela não arquivada)

Só se o usuário pedir uma janela que **não** está em `registros/`, rode a cadeia completa
(`coletar` → `classificar` → `agregar`), avisando que leva alguns minutos. Para drill-down do que já
foi postado, **sempre** use o snapshot.

## Modelos

As **conversas de construção/manutenção** (com o Felipe) usam sempre o **modelo mais recente e
robusto** (família Opus). A **execução do monitor** (rodada semanal: coleta → classificação →
agregação → notificação, na routine de nuvem) roda em **Sonnet** — esqueleto determinístico, só o
resíduo de classificação chama o modelo. O drill-down via Slack é leve (ler CSV e formatar) e não
exige fixar modelo.
