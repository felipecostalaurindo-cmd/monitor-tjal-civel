# monitor-tjal-civel — engine

Engine (stdlib Python, sem dependências) do Monitor das Câmaras Cíveis do TJ/AL.
Coleta acórdãos no e-SAJ cjsg, classifica por matéria/classe e gera a tabela de
percentuais por câmara para o Slack. Usado pela skill local `monitor-tjal-civel`
e pela routine de nuvem (claude.ai) que roda semanalmente.

Subcomandos: `coletar`, `classificar`, `agregar`, `filtrar`, `notificar`, `publicar`.
Rodar com `python3 monitor_tjal.py <subcomando> --help`.

## Registros semanais e drill-down via Slack (`@Claude`)

Cada rodada é arquivada em **`registros/<AAAA-MM-DD>/`** (`classificado.csv` + `resumo.json` +
`resumo.md` + `slack.txt`) — a **fonte do drill-down**. Respondendo à mensagem semanal no
`#tjal-camaras-civeis`, o usuário menciona **`@Claude`** e uma sessão do Claude Code na nuvem clona
este repo, roda `filtrar` sobre o snapshot e devolve os acórdãos. O playbook está em **`CLAUDE.md`**
(carregado automaticamente). **Atenção ao volume:** o cível tem milhares de acórdãos por rodada — o
playbook obriga a começar pela contagem/distribuição e a estreitar antes de listar números (o Slack
não entrega listas grandes). `filtrar` aceita `--com-ementa` (ementa sob demanda) e `--relator`.

O `publicar` copia o registro datado para `registros/<rótulo>/` e, com `--push`, commita e envia —
é o que mantém o drill-down alimentado a cada rodada (rodar do clone local; na nuvem o push é
bloqueado por permissão de escrita).
