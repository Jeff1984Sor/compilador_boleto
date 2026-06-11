# Conciliador de Boletos

Site interno que recebe **1 PDF com vários comprovantes** + **N PDFs de boletos**, casa cada boleto com seu comprovante pela **linha digitável** (extração via API Gemini) e devolve um **ZIP** com 1 PDF por boleto (boleto + comprovante mesclados). O nome do arquivo de saída é exatamente o nome original do boleto.

- Tela de **preview** antes do download para conferir os matches.
- Boletos sem comprovante vão para a pasta `sem_comprovante/` dentro do ZIP.
- Comprovantes órfãos (sem boleto) são listados no relatório `_conciliacao.txt`.

---

## Stack

- Python 3.10+
- Flask + Gunicorn
- `pypdf` para split/merge de PDFs
- API Gemini (Google) — `google-genai` (aceita PDF nativamente, sem conversão)
- Deploy em VM Linux do GCP (porta 9000)

---

**Repositório:** https://github.com/Jeff1984Sor/compilador_boleto.git

## 1. Rodar localmente

```bash
git clone https://github.com/Jeff1984Sor/compilador_boleto.git
cd compilador_boleto

python3 -m venv .venv
source .venv/bin/activate         # Windows PowerShell: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

cp .env.example .env
# edite .env e cole sua chave em GEMINI_API_KEY
# pegue uma em https://aistudio.google.com/app/apikey

python app.py
```

Abra http://localhost:9000.

---

## 2. Deploy na VM GCP (porta 9000)

A VM já tem **somente a porta 9000 liberada** no firewall. Como hoje há outro serviço escutando nela, precisamos identificá-lo e pará-lo antes.

### 2.1 Identificar e parar o serviço atual na porta 9000

```bash
# Quem está escutando na 9000?
sudo ss -tlnp | grep ':9000'
# Alternativa:
sudo lsof -i :9000

# Se for um serviço systemd, descubra o nome:
systemctl list-units --type=service --state=running

# Parar e desabilitar:
sudo systemctl stop <nome-do-servico>
sudo systemctl disable <nome-do-servico>

# Conferir que a porta liberou:
sudo ss -tlnp | grep ':9000'   # nao deve retornar nada
```

> Se o processo não for um service do systemd (ex.: rodando em screen/tmux/Docker), use `ps`, `docker ps` e mate manualmente. **Confirme com o time o que era esse serviço antes de remover.**

### 2.2 Instalar o conciliador

```bash
# Como root ou com sudo
sudo mkdir -p /opt/conciliador_boleto
sudo chown $USER /opt/conciliador_boleto

# Suba os arquivos (git clone)
cd /opt/conciliador_boleto
git clone https://github.com/Jeff1984Sor/compilador_boleto.git .

# Ambiente virtual e dependencias
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

# Configuracao
cp .env.example .env
nano .env   # cole GEMINI_API_KEY e gere um FLASK_SECRET_KEY aleatorio

# Diretorio de sessoes acessivel ao usuario do servico (ajuste se mudou)
sudo mkdir -p /tmp/conciliador_sessions
sudo chown www-data:www-data /tmp/conciliador_sessions
```

### 2.3 Subir como serviço systemd

```bash
sudo cp deploy/conciliador.service /etc/systemd/system/conciliador.service
# Ajuste o WorkingDirectory / User no arquivo se necessario:
sudo nano /etc/systemd/system/conciliador.service

# Permissoes dos arquivos do projeto (se usar User=www-data)
sudo chown -R www-data:www-data /opt/conciliador_boleto

sudo systemctl daemon-reload
sudo systemctl enable --now conciliador
sudo systemctl status conciliador
```

Verifique:

```bash
curl http://localhost:9000/health   # deve retornar {"ok": true}
```

Acesse de fora: `http://<IP_PUBLICO_DA_VM>:9000`

### 2.4 Logs

```bash
sudo journalctl -u conciliador -f
```

---

## 3. Estrutura do projeto

```
conciliador_boleto/
├── app.py                      # Flask: rotas /, /processar, /preview, /download, /health
├── conciliador/
│   ├── __init__.py
│   ├── gemini_client.py        # chamadas ao Gemini para extrair linha digitavel
│   ├── pdf_utils.py            # split_por_pagina, merge_pdfs
│   └── matcher.py              # conciliar() + montar_zip() + ResultadoConciliacao
├── templates/index.html        # tela unica: upload -> revisao -> download
├── static/
│   ├── style.css
│   └── app.js                  # fluxo de upload, render da tabela e modal de preview
├── deploy/conciliador.service  # unit systemd
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```

---

## 4. Como funciona o matching

1. O PDF de comprovantes é dividido por página (1 comprovante = 1 página).
2. Cada página de comprovante e cada PDF de boleto são enviados ao **Gemini** (PDF nativo) com prompt pedindo a linha digitável em JSON.
3. A linha digitável é normalizada (só dígitos) — 44/47/48 dígitos — e usada como **chave de match**.
4. Cada boleto:
   - Se a chave bate com uma página de comprovante: o boleto + a página são mesclados num PDF com **o mesmo nome do boleto original**.
   - Se não bate: o boleto vai para a pasta `sem_comprovante/` do ZIP.
5. A tela de revisão mostra o status de cada boleto e permite preview antes do download.

---

## 5. Variáveis de ambiente

| Variável | Default | Descrição |
| --- | --- | --- |
| `GEMINI_API_KEY` | _(obrigatória)_ | Chave da API Gemini |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Modelo usado para extração |
| `PORT` | `9000` | Porta HTTP |
| `SESSIONS_DIR` | `/tmp/conciliador_sessions` | Onde os PDFs intermediários ficam |
| `SESSION_TTL_MINUTES` | `30` | Tempo até a sessão expirar e ser apagada |
| `FLASK_SECRET_KEY` | _(troque)_ | Chave secreta do Flask |

---

## 6. Limites

- Upload total limitado a **200 MB** (ajuste `MAX_CONTENT_LENGTH` em `app.py`).
- Cada chamada ao Gemini gasta tokens; rodar em paralelo (até 6 threads) reduz tempo total. O free tier de `gemini-2.0-flash` aguenta 15 RPM/1M TPM — sobra pra esse uso.
- Boletos com layout muito atípico podem falhar na extração da linha digitável — caem em `sem_comprovante/`.
