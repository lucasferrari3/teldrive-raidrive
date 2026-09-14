# Teldrive RaiDrive

Mount híbrido de [Teldrive](https://github.com/tgdrive/teldrive) ou WebDAV, com cache em blocos e prefetch sequencial no estilo RaiDrive. Pensado para streaming (Jellyfin, Plex, etc.).

Não é afiliado ao RaiDrive. Só o modelo de cache/prefetch.

[![CI](https://github.com/lucasferrari3/teldrive-raidrive/actions/workflows/ci.yml/badge.svg)](https://github.com/lucasferrari3/teldrive-raidrive/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

## Backends e sistemas

| Backend (`.env`) | Origem | Escrita |
|---|---|---|
| `BACKEND=teldrive` (padrão) | [Teldrive](https://github.com/tgdrive/teldrive) HTTP API | Sim (`READ_ONLY=0`) |
| `BACKEND=webdav` | WebDAV (PROPFIND / Range GET; PUT / MKCOL / DELETE / MOVE) | Sim (`READ_ONLY=0`) |

| SO | Driver de mount |
|---|---|
| **Windows** | [Dokany](https://github.com/dokan-dev/dokany) (`dokan2.dll`) |
| **Linux** / WSL2 | FUSE 3 (`pyfuse3`) |

Teldrive usa a mesma API do rclone — [guia oficial](https://teldrive-docs.pages.dev/docs/guides/rclone).

## Requisitos

- Python 3.10+
- Credenciais do backend (Teldrive **ou** WebDAV)

```bash
python -m pip install -r requirements.txt
```

Ou, a partir da raiz do projeto:

```bash
python -m pip install -e .
```

### Windows (Dokany)

1. Instale o Dokany 2:

   ```powershell
   winget install dokan-dev.Dokany
   # ou: choco install dokany2
   ```

2. Reinicie se o instalador pedir (driver).
3. Confirme: `dokanctl.exe /v`

### Linux (FUSE)

```bash
sudo apt install fuse3 python3-pyfuse3
python -m pip install -r requirements-linux.txt
# ou: python -m pip install -e ".[linux]"
```

Precisa de `/dev/fuse` (WSL2 / VM / bare metal — **não** WSL1).

## Início rápido

```bash
cp .env.example .env
```

No Windows: `copy .env.example .env`

Edite o `.env`:

- Teldrive: `TELDRIVE_API_HOST` + `TELDRIVE_ACCESS_TOKEN`
- WebDAV: `BACKEND=webdav` + `WEBDAV_URL` / `WEBDAV_USER` / `WEBDAV_PASSWORD`

**Não commite o `.env`.** Ele já está no `.gitignore`.

### Testar a API (qualquer SO)

```bash
python -m raidrive probe -v
```

### Montar no Windows

```powershell
# .env: MOUNT_POINT=T:
python -m raidrive mount
# ou:
python -m raidrive mount T:
```

A letra `T:` aparece no Explorer. Desmontar: `Ctrl+C`, ou:

```powershell
dokanctl /u T:
```

### Montar no Linux

```bash
python -m raidrive mount /mnt/telegram
# ou use MOUNT_POINT do .env
```

Desmontar: `Ctrl+C`, ou `fusermount3 -u /mnt/telegram`.

## Access token (Teldrive)

1. Abra a UI web do Teldrive e faça login.
2. DevTools → Cookies (ou Cookie Editor).
3. Copie o cookie `access_token`.
4. Coloque em `TELDRIVE_ACCESS_TOKEN`.

Ou use `TELDRIVE_API_KEY` (`X-Api-Key`).

## Configuração (`.env`)

| Variável | Padrão | Descrição |
|---|---|---|
| `BACKEND` | `teldrive` | `teldrive` ou `webdav` |
| `TELDRIVE_API_HOST` | — | URL base Teldrive |
| `TELDRIVE_ACCESS_TOKEN` | — | Cookie de sessão |
| `TELDRIVE_API_KEY` | — | API key opcional |
| `WEBDAV_URL` | — | URL WebDAV (modo webdav) |
| `WEBDAV_USER` / `WEBDAV_PASSWORD` | — | Auth Basic |
| `MOUNT_POINT` | `./mnt` | Linux: pasta. Windows: letra (`T:`) |
| `READ_ONLY` | `1` | `0` = cria/edita/apaga/renomeia (Teldrive e WebDAV; upload ao fechar) |
| `UPLOAD_CHUNK_MIB` | `64` | Partes de upload (Teldrive RW) |
| `CHANNEL_ID` | `0` | Canal Telegram (0 = padrão) |
| `DOKAN_TIMEOUT_MS` | `300000` | Timeout Dokany por request (ms) |
| `DOKAN_BRIDGE_WORKERS` | `4` | Workers Python p/ I/O (Windows) |
| `DOKAN_SINGLE_THREAD` | — | `1` serializa callbacks Dokany (mais estável) |
| `BLOCK_MIB` | `2` | Tamanho do bloco Range/cache (MiB). Streaming: 4 |
| `PREFETCH_BLOCKS` | `8` | Blocos à frente; 4 é um bom padrão |
| `PREFETCH_AFTER` | `2` | Prefetch após N leituras sequenciais |
| `FAST_FIRST_MIB` | `1` | Janela inicial no cold start (Play mais rápido) |
| `PREFETCH_WORKERS` | auto | Threads de download/prefetch (cap 4) |
| `CACHE_DIR` | vazio | Cache em disco (`.blk` por bloco); vazio = só RAM |
| `CACHE_MIB` | `2048` | Teto do cache em disco (MiB) |
| `CACHE_READ_TTL` | `12:00:00` | Validade dos blocos |
| `CACHE_CLEAN_INTERVAL` | `1:00:00` | Limpeza periódica |
| `RAM_CACHE_MIB` | `512` | Cache quente em RAM |
| `META_TTL` | `300` | TTL de listagens (s) |
| `NEGATIVE_META_TTL` | `META_TTL` | TTL de paths inexistentes |
| `PAGE_SIZE` | `500` | Página da API Teldrive (máx. 1000) |
| `MODE` | — | Permissões octais (Linux) |
| `MOUNT_UID` / `MOUNT_GID` | user | Dono (Linux) |
| `ALLOW_OTHER` | `0` | Outros users (Linux / fuse.conf) |

Veja `.env.example` para um template completo.

### Exemplo Teldrive (Windows)

```env
BACKEND=teldrive
TELDRIVE_API_HOST=http://127.0.0.1:8080
TELDRIVE_ACCESS_TOKEN=eyJ...
MOUNT_POINT=T:
READ_ONLY=1
BLOCK_MIB=4
CACHE_DIR=D:\cache-tg
```

### Exemplo WebDAV

```env
BACKEND=webdav
WEBDAV_URL=http://127.0.0.1:5000/webdav
WEBDAV_USER=admin
WEBDAV_PASSWORD=changeme
MOUNT_POINT=T:
READ_ONLY=0
BLOCK_MIB=2
PREFETCH_BLOCKS=4
CACHE_DIR=D:\cache-webdav
```

## Comandos

```bash
python -m raidrive probe [-v]
python -m raidrive mount [T:|/mnt/telegram] [-v]
python -m raidrive --version
```

## Observações

- Um mount = um backend (`BACKEND`). Cache em disco usa namespace separado por host/URL.
- Teldrive: com `READ_ONLY=0` cria/edita/apaga/renomeia (upload ao fechar). Arquivo grande → reenvio completo.
- WebDAV: com `READ_ONLY=0` o mesmo (PUT / MKCOL / DELETE / MOVE). Arquivo grande → PUT completo ao fechar. O servidor precisa permitir escrita. No Windows, `DOKAN_TIMEOUT_MS` também vale para o upload no close.
- No Windows o mount usa **Dokany**. Prefetch roda num pool próprio (não na thread nativa).
- Cache em disco: um `.blk` por bloco Range. O cliente nunca baixa mais que o tamanho do bloco (protege contra WebDAV que ignora `Range`).
- Em hosts sem FUSE/Dokany, use só `probe`.

## Licença

[MIT](LICENSE)
