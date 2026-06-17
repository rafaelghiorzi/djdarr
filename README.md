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
    aprovar, rejeitar, marcar como tocada, re-tentar).
  - `worker` — thread em background que processa a fila de aprovados e baixa
    cada faixa com `yt-dlp` (busca em YouTube e SoundCloud, escolhe o melhor
    resultado por fuzzy matching com `rapidfuzz`).
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
# É aqui que as músicas baixadas aparecem.
DOWNLOADS_PATH=/caminho/para/sua/pasta/de/downloads

# Formato de áudio: wav (sem perdas, recomendado) ou mp3
AUDIO_FORMAT=wav

# ── Limites de fila ───────────────────────────────────────────────────
MAX_QUEUE_SIZE=50        # máximo de pedidos pendentes simultâneos
MAX_PENDING_PER_IP=3     # máximo de pedidos pendentes por IP
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

## Publicando na internet (produção)

Em produção, **não** se expõe as portas diretamente — o acesso passa pelo
Cloudflare Tunnel. Recomenda-se remover/comentar as linhas `ports:` do
`docker-compose.yml` e deixar o `cloudflared` rotear o tráfego.

### 1. Suba os containers

```bash
docker compose up --build -d
```

### 2. Rode o Cloudflare Tunnel (em outro terminal)

Com o `cloudflared` já autenticado e um túnel criado, configure o ingress para
apontar cada hostname ao container correspondente. Exemplo de `config.yml` do
`cloudflared`:

```yaml
tunnel: <ID-do-tunnel>
credentials-file: /caminho/para/<ID-do-tunnel>.json

ingress:
  # Página pública dos fãs → Container A
  - hostname: fans.seudominio.com
    service: http://localhost:8000

  # Painel privado do DJ → Container B (proteja com Cloudflare Access!)
  - hostname: painel.seudominio.com
    service: http://localhost:8501

  - service: http_status:404
```

E então, em um terminal separado:

```bash
cloudflared tunnel run <nome-ou-ID-do-tunnel>
```

> A porta interna `8001` (`internal:app`) **nunca** deve aparecer no ingress do
> túnel nem nos `ports:` do compose — ela só é acessível pela rede interna do
> docker e é o único caminho do Container A para o banco.

### 3. Proteja o painel com Cloudflare Access

No painel Zero Trust da Cloudflare, crie uma aplicação do tipo *Self-hosted*
para `painel.seudominio.com` e adicione uma política que só libere o seu e-mail.
Sem isso, o painel do DJ ficaria aberto na internet.

---

## Fluxo de uso

1. **O fã** abre a página pública, digita o nome da música (ex.:
   `Thiaguinho Cheia de Manias`) ou cola um link do YouTube/SoundCloud, resolve
   o Turnstile e envia.
2. **O DJ** vê o pedido aparecer na coluna **Pendentes** do painel e clica em
   **Aprovar** ou **Rejeitar**.
3. Ao aprovar, o **worker** baixa a faixa automaticamente. O status caminha por
   `approved → downloading → ready` (ou `failed`, com botão de **re-tentar**).
4. Depois de tocar a música, o DJ marca como **tocada**.

O download usa busca simultânea no **YouTube** e **SoundCloud**, comparando os
resultados com a query por fuzzy matching e baixando o mais parecido, na melhor
qualidade disponível, convertido para o formato definido em `AUDIO_FORMAT`.

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
│   ├── requirements.txt
│   └── static/index.html       # Página do fã
│
└── painel/                     # Container B — painel do DJ + API + worker
    ├── Dockerfile
    ├── start.sh                # Sobe internal:8001 e api:8501
    ├── api.py                  # FastAPI :8501 — painel HTML + API de gestão
    ├── internal.py             # FastAPI :8001 — /internal/submit (rede interna)
    ├── worker.py               # Thread que baixa a fila de aprovados
    ├── db.py                   # SQLite (estado dos pedidos)
    ├── requirements.txt
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
