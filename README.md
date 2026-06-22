# monitor-tjal-civel — engine

Engine (stdlib Python, sem dependências) do Monitor das Câmaras Cíveis do TJ/AL.
Coleta acórdãos no e-SAJ cjsg, classifica por matéria/classe e gera a tabela de
percentuais por câmara para o Slack. Usado pela skill local `monitor-tjal-civel`
e pela routine de nuvem (claude.ai) que roda semanalmente.

Subcomandos: `coletar`, `classificar`, `agregar`, `filtrar`, `notificar`.
Rodar com `python3 monitor_tjal.py <subcomando> --help`.
