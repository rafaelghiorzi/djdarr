# 🎧 DJ Setlist Automator

Sistema para baixar e organizar músicas automaticamente para setlists de DJ.

---

## Pré-requisitos

- **Python 3.10+**
- **ffmpeg** instalado e no PATH (necessário para conversão de áudio)

### Instalar ffmpeg

**macOS:**
```bash
brew install ffmpeg
```

**Ubuntu/Debian:**
```bash
sudo apt install ffmpeg
```

**Windows:**
Baixe em https://ffmpeg.org/download.html e adicione ao PATH.

---

## Instalação

```bash
# 1. Clone ou extraia o projeto
cd dj-setlist

# 2. Crie um ambiente virtual (recomendado)
python -m venv .venv
source .venv/bin/activate      # Linux/macOS
# .venv\Scripts\activate       # Windows

# 3. Instale as dependências
pip install -r requirements.txt
```

---

## Configuração

### 1. Caminho das músicas (`.env`)

Edite o arquivo `.env` na raiz do projeto:

```env
# Onde ficam as pastas de músicas (use o caminho real da sua máquina)
MUSIC_BASE_PATH=~/Músicas/DJ

# Formato do arquivo de saída
AUDIO_FORMAT=mp3

# Qualidade (0 = melhor, 9 = menor)
AUDIO_QUALITY=0
```

### 2. Pastas / Categorias (`config/folders.yaml`)

Edite para refletir sua estrutura de pastas:

```yaml
folders:
  - label: "🕺 Funk"
    folder: "funk"
  - label: "💿 Nostálgicas"
    folder: "nostalgicas"
  # Adicione ou remova à vontade
```

O sistema criará automaticamente as subpastas dentro de `MUSIC_BASE_PATH`.

---

## Rodando o app

```bash
streamlit run app.py
```

O navegador abrirá em `http://localhost:8501`.

---

## Como usar

1. **Música + Artista**: Digite a query mais próxima do que quer baixar.
   - Ex: `Thiaguinho Cheia de Manias`
   - Ou cole um link direto: `https://youtu.be/...`

2. **Pasta de destino**: Escolha a categoria na qual a música será salva.

3. **Nome do arquivo**: Como o `.mp3` será salvo.
   - Ex: `Thiaguinho - Cheia de Manias`

4. Clique em **BAIXAR MÚSICA**.

O sistema busca no **YouTube** e **SoundCloud** simultaneamente, compara qual resultado é mais parecido com a query (via fuzzy matching) e baixa o melhor.

---

## Baixar playlist (set) do SoundCloud

Na seção **☁ Baixar Playlist do SoundCloud (.WAV)**:

1. Cole o link de um *set* do SoundCloud, no formato:
   `https://soundcloud.com/usuario/sets/nome-da-playlist`
2. Escolha a pasta de destino.
3. Clique em **BAIXAR PLAYLIST**.

O sistema baixa **todas as faixas da playlist** na melhor qualidade de áudio disponível,
convertendo cada uma para **.wav** (sem compressão), com metadados embutidos
(título, artista, álbum = nome da playlist, número da faixa, data e link original).
As faixas são salvas em uma subpasta nomeada com o título da playlist, dentro da
pasta escolhida.

> ⚠️ Arquivos `.wav` são bem maiores que `.mp3`. Playlists grandes podem levar
> bastante tempo e ocupar vários gigabytes de espaço em disco.

---

## Estrutura de arquivos

```
dj-setlist/
├── app.py                  # Frontend Streamlit
├── logger_setup.py         # Configuração de logging
├── requirements.txt
├── .env                    # Variáveis de ambiente (não subir no git!)
├── config/
│   ├── __init__.py
│   ├── loader.py           # Carrega .env e folders.yaml
│   └── folders.yaml        # Definição das categorias/pastas
├── downloader/
│   ├── __init__.py
│   └── core.py             # Lógica de busca, comparação e download
└── logs/
    └── setlist.log         # Logs rotativos
```

---

## Logs

Os logs ficam em `logs/setlist.log` (configurável no `.env`).
Você também pode visualizá-los diretamente no app em **"📋 Ver logs recentes"**.

Para aumentar o detalhe dos logs, altere no `.env`:
```env
LOG_LEVEL=DEBUG
```

---

## Solução de problemas

| Problema | Solução |
|----------|---------|
| `ffmpeg not found` | Instale o ffmpeg e garanta que está no PATH |
| Nenhum resultado encontrado | Tente uma query mais específica ou use um link direto |
| Erro de permissão na pasta | Verifique se `MUSIC_BASE_PATH` existe e tem permissão de escrita |
| SoundCloud sem resultado | Normal — nem todas as músicas estão no SoundCloud; o YouTube é usado como fallback |
