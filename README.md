# Cronobs — Cron Observatory for Hermes Agent

Dashboard administrativo de cron jobs do Hermes Agent. Visualiza, edita, pausa, executa, duplica, move e restaura jobs de todos os profiles via interface web local.

## Features

- **Cards** — visualização detalhada com collapse, métricas e ações por job
- **Kanban** — arraste e solte entre profiles, status, deliver e dimensões customizáveis
- **Lista** — tabela compacta com sorting duplo e colunas reordenáveis
- **Edição segura** — preview de diff antes de salvar, backup automático, rollback por timestamp
- **Multi-profile** — jobs de todos os profiles em uma única tela, com filtro multi-select
- **Dark/Light mode** — automático por horário ou manual, persiste no localStorage
- **Font scaling** — ajuste de tamanho com botões A-/A+ ou atalhos `Cmd+=` / `Cmd+-`
- **Auto-refresh** — dados a cada 30s, countdown do próximo job em tempo real
- **Hermes Dashboard integration** — aparece como aba no `hermes dashboard`

## Install

```bash
hermes plugins install hendrixfreire/cronobs --enable
```

## Usage

### Iniciar o servidor

```bash
hermes cronobs start
```

Abre automaticamente o browser em `http://127.0.0.1:8700`.

### Comandos CLI

```bash
hermes cronobs start     # Inicia o servidor na porta 8700
hermes cronobs stop      # Para o servidor
hermes cronobs status    # Mostra se está rodando
```

### Slash command

No chat do Hermes:

```
/cronobs
```

Mostra status, URL e resumo dos jobs ativos.

### Hermes Dashboard

O plugin registra uma aba "Cronobs" no `hermes dashboard`. Para ativar:

```bash
hermes dashboard
```

A aba aparece na sidebar e carrega o cronobs via iframe. O servidor precisa estar rodando (`hermes cronobs start`).

## Keyboard shortcuts

| Atalho | Ação |
|--------|------|
| `Cmd + =` | Aumentar fonte |
| `Cmd + -` | Diminuir fonte |
| `Cmd + 0` | Resetar fonte (100%) |

## Configuration

### Porta customizada

```bash
hermes cronobs start --port 9999
# ou
CRONOBS_PORT=9999 hermes cronobs start
```

### Data sources

O dashboard lê jobs de:

```
~/.hermes/cron/jobs.json                          # profile default
~/.hermes/profiles/<nome>/cron/jobs.json           # outros profiles
```

## Architecture

```
~/.hermes/plugins/cronobs/
├── plugin.yaml          # Manifesto do plugin
├── __init__.py          # register(ctx) — CLI + slash command
├── cli.py               # argparse + handlers (start/stop/status)
├── server.py            # HTTP server + HTML embutido (porta 8700)
└── dashboard/
    ├── manifest.json    # Registro no Hermes Dashboard
    └── index.js         # iframe component
```

O `server.py` é um único arquivo Python com zero dependências externas — usa apenas `http.server` do stdlib. Todo o HTML, CSS e JavaScript está embutido como string.

## Requirements

- Python 3.10+
- Hermes Agent instalado
- macOS, Linux ou Windows

## License

MIT
