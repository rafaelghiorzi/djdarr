# 🎧 Djdarr

Sistema de **pedidos de música ao vivo para DJs**. Os fãs enviam pedidos por uma
página pública; o DJ aprova ou rejeita cada um a partir de um painel privado, e
um worker baixa automaticamente as faixas aprovadas (YouTube / SoundCloud) na
pasta de downloads do DJ.

---

## Arquitetura

O projeto roda em **dois containers** orquestrados via `docker-compose`:

```
                          Internet (fãs)
                                │
                                ▼
                    ┌───────────────────────┐
   Cloudflare       │  fans  (Container A)  │   página pública
   Tunnel  ───────> │  FastAPI :8000        │   protegida por Turnstile
                    │  POST /submit         │
                    └───────────┬───────────┘
                                │  rede interna do docker (http)
                                ▼  POST /internal/submit
                    ┌─────────────────────────┐
   Cloudflare       │  painel (Container B)   │   painel privado do DJ
   Tunnel + Access >│  api    :8501  <────────┼── HTML + API de gestão
                    │  internal :8001 (só     │   (aprovar/rejeitar/retry)
                    │           rede interna) │
                    │  worker  (thread)       │── baixa via yt-dlp → /downloads
                    │  SQLite  /data/djdarr.db│
                    └─────────────────────────┘
```

- **`fans/` (Container A)** — página pública onde o fã digita o nome da música
  ou cola um link do YouTube/SoundCloud. Valida o **Cloudflare Turnstile**
  (anti-bot), aplica rate limiting por IP e repassa o pedido ao Container B
  pela rede interna. Não tem acesso ao banco nem aos downloads.
- **`painel/` (Container B)** — concentra três coisas:
  - `internal:8001` — único endpoint (`/internal/submit`) que o Container A usa
    para inserir pedidos. **Não exposto** ao host nem ao túnel.
  - `api:8501` — painel HTML do DJ + API de gestão (listar pendentes/aprovados,
    aprovar, rejeitar, marcar como tocada, re-tentar, ligar/desligar o daemon
    de download).
  - `worker` — **daemon opcional** (desligado por padrão, ligado no painel)
    com um **pool de 2–3 threads** (`DOWNLOAD_WORKERS`) que processam a fila
    de aprovados em paralelo, baixando cada faixa com `yt-dlp` (busca em
    YouTube e SoundCloud, escolhe o melhor resultado por fuzzy matching com
    `rapidfuzz`). Desligar o daemon não cancela downloads em andamento — só
    para de puxar novos itens da fila. Todo arquivo baixado é renomeado para
    `Música - Artista`, convertido para **MP3** com a capa do álbum embutida
    (tags ID3, via `mutagen`) e uma cópia da capa fica disponível para o
    painel em `/api/thumb/{id}`.
  - `SQLite` em `/data/djdarr.db` (volume persistente) guarda o estado de cada
    pedido: `pending → approved → downloading → ready/failed → played`.

---

## Pré-requisitos

- **Docker** e **Docker Compose**
- Uma conta **Cloudflare** (gratuita já basta) para:
  - **Turnstile** — chave de site/segredo do widget anti-bot da página dos fãs.
  - **Cloudflare Tunnel** (`cloudflared`) — para expor os serviços à internet
    sem abrir portas no roteador.
  - **Cloudflare Access** (Zero Trust) — para proteger o painel do DJ (`:8501`),
    de modo que só você consiga acessá-lo.
- Um **domínio** gerenciado pela Cloudflare (ex.: `fans.seudominio.com` para os
  fãs e `painel.seudominio.com` para o DJ).
- `ffmpeg` — **não precisa instalar no host**; já vem na imagem do Container B
  (usado pelo `yt-dlp` para converter o áudio).

> **Dá pra rodar 100% local, sem domínio nem Cloudflare** — veja a seção
> [Rodando localmente](#rodando-localmente). O Cloudflare só é necessário para
> publicar o serviço na internet de forma segura.

As dependências Python de cada serviço são gerenciadas com **[uv](https://docs.astral.sh/uv/)**
(`pyproject.toml` + `uv.lock`) — os Dockerfiles já instalam o `uv` e rodam
`uv sync --locked` sozinhos, **você não precisa instalar nada disso no host**
para rodar via Docker. Só instale o `uv` localmente se for rodar/editar um dos
serviços fora do container (veja [Desenvolvimento local sem
Docker](#desenvolvimento-local-sem-docker)).

---

## Configuração

Crie um arquivo `.env` na raiz do projeto (ele está no `.gitignore`):

```env
# ── Cloudflare Turnstile ──────────────────────────────────────────────
# Obtenha em: dash.cloudflare.com → Turnstile → Add widget
TURNSTILE_SITE_KEY=sua_site_key
TURNSTILE_SECRET_KEY=seu_secret_key

# ── CORS cosmético (opcional) ─────────────────────────────────────────
# Origem da página dos fãs. Deixe vazio para aceitar qualquer origem
# (a defesa real é o Turnstile). Em produção, use seu domínio.
FAN_PAGE_ORIGIN=https://fans.seudominio.com

# ── Volume de downloads ───────────────────────────────────────────────
# Pasta LOCAL (do host) que será montada em /downloads no Container B.
# É aqui que as músicas baixadas aparecem, já nomeadas "Música - Artista.mp3".
DOWNLOADS_PATH=/caminho/para/sua/pasta/de/downloads

# Formato de áudio: mp3 (recomendado — grava tags ID3 + capa do álbum
# embutida) ou wav (sem perdas, mas sem capa embutida no arquivo)
AUDIO_FORMAT=mp3

# ── Limites de fila ───────────────────────────────────────────────────
MAX_QUEUE_SIZE=50        # máximo de pedidos pendentes simultâneos
MAX_PENDING_PER_IP=3     # máximo de pedidos pendentes por IP

# ── Daemon de download ───────────────────────────────────────────────
# Quantos workers baixam em paralelo quando o daemon está ligado (2 ou 3).
# O daemon fica desligado até você ligá-lo no painel — pedidos aprovados
# ficam represados até lá.
DOWNLOAD_WORKERS=2
```

### Chaves de teste do Turnstile

Para testar localmente sem registrar um widget real, a Cloudflare oferece chaves
que **sempre passam** na validação:

```env
TURNSTILE_SITE_KEY=1x00000000000000000000AA
TURNSTILE_SECRET_KEY=1x0000000000000000000000000000000AA
```

---

## Rodando localmente

Sem Cloudflare nem domínio — útil para desenvolvimento e testes:

```bash
# 1. Configure o .env (use as chaves de teste do Turnstile acima)

# 2. Suba os dois containers
docker compose up --build
```

Acesse:

- **Página dos fãs:** http://localhost:8000
- **Painel do DJ:** http://localhost:8501

As músicas aprovadas serão baixadas na pasta apontada por `DOWNLOADS_PATH`.

> Localmente, o painel **não** fica atrás do Cloudflare Access, ou seja, qualquer
> pessoa na sua rede que alcance a porta `8501` consegue abrir o painel. Em
> produção, isso é resolvido pelo Cloudflare Access (veja abaixo).

---

## Desenvolvimento local sem Docker

Só necessário se você for rodar/editar `fans/` ou `painel/` diretamente no
host (ex.: autocomplete/lint no editor, debugar sem rebuildar a imagem).
Requer [uv](https://docs.astral.sh/uv/getting-started/installation/) instalado.

```bash
# fans/
cd fans
uv sync              # cria .venv/ e instala as dependências do uv.lock
uv run uvicorn main:app --reload --port 8000

# painel/ (em outro terminal)
cd painel
uv sync
DB_PATH=./djdarr.db DOWNLOADS_PATH=./downloads uv run uvicorn api:app --reload --port 8501
```

Depois de adicionar/remover uma dependência no `pyproject.toml`, rode
`uv lock` na mesma pasta para atualizar o `uv.lock` (ele **é** versionado —
commite junto com o `pyproject.toml`).

---

## Publicando na internet (produção)

Em produção, **não** se expõe as portas diretamente — o acesso passa pelo
Cloudflare Tunnel via um terceiro serviço no `docker-compose.yml`
(`cloudflared`), que conecta ao túnel usando um **token** em vez de um
`config.yml` local. As linhas `ports:` de `fans_page` e `dj_panel` já vêm
comentadas no compose para produção; descomente-as apenas se precisar testar
via `localhost` no mesmo host.

Este projeto usa um túnel criado pelo **dashboard Zero Trust** (não pela CLI),
então o roteamento hostname → serviço é configurado no próprio dashboard, não
em um `config.yml`.

### 1. Configure os hostnames públicos no dashboard

Zero Trust → **Networks → Tunnels** → seu túnel → aba **Public Hostname** →
**Add a public hostname**, duas vezes:

| Hostname público | Service |
|---|---|
| `fans.rafaelghiorzi.org` | `HTTP` → `fans_page:8000` |
| `painel.rafaelghiorzi.org` | `HTTP` → `dj_panel:8501` |

Use o nome do serviço do compose (`fans_page`, `dj_panel`), não `localhost` —
o `cloudflared` roda como container na mesma rede docker (`djdarr_net`) e
resolve os outros serviços pelo nome. O Cloudflare cria o registro DNS
automaticamente ao salvar cada hostname.

> A porta interna `8001` (`internal:app`) **nunca** deve virar um hostname
> público — ela só é acessível pela rede interna do docker e é o único
> caminho do Container A para o banco.

### 2. Pegue o token do túnel

Na mesma tela do túnel → **Configure** (ou no passo de instalação do
conector) → copie o token — é a string longa em base64 depois de `--token` no
comando de exemplo. Cole em `CLOUDFLARE_TUNNEL_TOKEN` no `.env`.

### 3. Proteja o painel com Cloudflare Access

Zero Trust → **Access → Applications** → **Add an application** →
*Self-hosted*, domínio `painel.rafaelghiorzi.org`, com uma política que libere
só o seu e-mail. **Faça isso antes do passo 4** — sem Cloudflare Access, o
painel do DJ (aprovar/rejeitar pedidos, ver IPs, ver caminhos de arquivo) fica
aberto para qualquer pessoa na internet assim que o hostname existir.

### 4. Suba tudo

```bash
docker compose up --build -d
```

O `cloudflared` conecta ao túnel automaticamente; `docker compose logs -f cloudflared`
mostra o status da conexão. A partir daí, `fans.rafaelghiorzi.org` e
`painel.rafaelghiorzi.org` já respondem.

### Antes de ir ao ar, confira

- [ ] `CLOUDFLARE_TUNNEL_TOKEN` preenchido no `.env`.
- [ ] Cloudflare Access protegendo `painel.rafaelghiorzi.org` (passo 3).
- [ ] `FAN_PAGE_ORIGIN=https://fans.rafaelghiorzi.org` no `.env` (com `https://`).
- [ ] No widget Turnstile (dash.cloudflare.com → Turnstile), o domínio
      `fans.rafaelghiorzi.org` está na lista de domínios permitidos — senão o
      widget não carrega/valida no domínio público.
- [ ] `DOWNLOADS_PATH` aponta para a pasta onde você realmente quer que as
      músicas caiam (a pasta que o seu software de DJ vai ler).

---

## Fluxo de uso

1. **O fã** abre a página pública, digita o nome da música (ex.:
   `Thiaguinho Cheia de Manias`) ou cola um link do YouTube/SoundCloud, resolve
   o Turnstile e envia.
2. **O DJ** vê o pedido aparecer na coluna **Pendentes** do painel e clica em
   **Aprovar** ou **Rejeitar**.
3. Ao aprovar, o pedido entra na fila **Fila & baixando**. Se o **daemon**
   estiver ligado (toggle no topo do painel), um dos workers pega o pedido e
   baixa automaticamente; se estiver desligado, o pedido fica represado até
   você ligar. O status caminha por `approved → downloading → ready` (ou
   `failed`, com botão de **re-tentar**).
4. Depois de tocar a música, o DJ marca como **tocada**.

O download usa busca simultânea no **YouTube** e **SoundCloud**, comparando os
resultados com a query por fuzzy matching e baixando o mais parecido, na melhor
qualidade disponível. O arquivo final é sempre renomeado para
**`Música - Artista.mp3`**, com essas mesmas tags gravadas no ID3 e a capa do
álbum embutida (baixada do YouTube/SoundCloud).

> A qualidade do "nome limpo" depende do que a plataforma de origem expõe.
> Quando o título do vídeo/faixa segue a convenção `Artista - Música`, a
> separação é exata. Sem esse padrão, o nome do canal/perfil é usado como
> artista. Marcadores comuns como `(Official Video)`, `(Lyrics)`, `[HD]`,
> `(4K Remaster)` etc. são removidos automaticamente.

---

## Estrutura do projeto

```
djdarr/
├── docker-compose.yml          # Orquestra os dois containers + volumes/rede
├── .env                        # Configuração (não versionado)
│
├── fans/                       # Container A — página pública dos fãs
│   ├── Dockerfile
│   ├── main.py                 # FastAPI :8000 — /submit + Turnstile + rate limit
│   ├── pyproject.toml          # Dependências (gerenciadas com uv)
│   ├── uv.lock
│   └── static/index.html       # Página do fã
│
└── painel/                     # Container B — painel do DJ + daemon de download
    ├── Dockerfile
    ├── start.sh                # Sobe internal:8001 e api:8501
    ├── api.py                  # FastAPI :8501 — painel HTML + API de gestão
    ├── internal.py             # FastAPI :8001 — /internal/submit (rede interna)
    ├── worker.py               # Pool de workers (daemon) que baixa a fila de aprovados
    ├── db.py                   # SQLite (estado dos pedidos)
    ├── pyproject.toml          # Dependências (gerenciadas com uv)
    ├── uv.lock
    ├── downloader/
    │   ├── __init__.py
    │   └── core.py             # Busca (YouTube/SoundCloud) + fuzzy + download
    └── static/panel.html       # Painel do DJ
```

---

## Solução de problemas

| Problema | Solução |
|----------|---------|
| Turnstile sempre falha | Confira `TURNSTILE_SITE_KEY`/`TURNSTILE_SECRET_KEY`; para testar use as chaves `1x...` que sempre passam |
| Pedido não chega ao painel | Verifique se os dois containers estão na mesma rede do compose e se o Container B está de pé (`docker compose logs dj_panel`) |
| Download falha (`failed`) | Veja os logs do Container B; faça **re-tentar** no painel. Nem toda música existe no SoundCloud — o YouTube é o fallback |
| Músicas não aparecem na pasta | Confirme que `DOWNLOADS_PATH` aponta para uma pasta existente e com permissão de escrita |
| Painel aberto na internet | Falta configurar o Cloudflare Access para `:8501` |
| `database is locked` | Raro — o SQLite usa WAL + `busy_timeout`; reinicie o Container B se persistir |
