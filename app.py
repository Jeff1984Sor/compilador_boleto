"""App Flask: upload, processamento, preview e download do ZIP final."""

from __future__ import annotations

import io
import json
import logging
import os
import shutil
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv
from flask import (
    Flask,
    abort,
    jsonify,
    render_template,
    request,
    send_file,
    send_from_directory,
)

from conciliador import matcher

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_UPLOAD_MB", "500")) * 1024 * 1024
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-key-trocar-em-producao")

SESSIONS_DIR = Path(os.environ.get("SESSIONS_DIR", "/tmp/conciliador_sessions"))
SESSION_TTL_MINUTES = int(os.environ.get("SESSION_TTL_MINUTES", "30"))
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


def _limpar_sessoes_antigas():
    """Apaga diretorios de sessao mais antigos que SESSION_TTL_MINUTES."""
    if not SESSIONS_DIR.exists():
        return
    limite = time.time() - SESSION_TTL_MINUTES * 60
    for sub in SESSIONS_DIR.iterdir():
        if sub.is_dir() and sub.stat().st_mtime < limite:
            try:
                shutil.rmtree(sub)
                log.info("Sessao expirada removida: %s", sub.name)
            except OSError as e:
                log.warning("Falha ao remover sessao %s: %s", sub.name, e)


def _sessao_dir(session_id: str) -> Path:
    # Bloqueia path traversal — session_id deve ser apenas hex/uuid
    if not session_id.replace("-", "").isalnum():
        abort(400, "session_id invalido")
    p = SESSIONS_DIR / session_id
    if not p.exists() or not p.is_dir():
        abort(404, "sessao nao encontrada ou expirada")
    return p


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/processar", methods=["POST"])
def processar():
    _limpar_sessoes_antigas()

    comprovantes_file = request.files.get("comprovantes")
    boletos_files = request.files.getlist("boletos")

    if not comprovantes_file or not comprovantes_file.filename:
        return jsonify(erro="Envie o PDF de comprovantes (campo 'comprovantes')."), 400
    if not boletos_files:
        return jsonify(erro="Envie pelo menos 1 boleto (campo 'boletos')."), 400

    comprovantes_pdf = comprovantes_file.read()
    boletos = [(f.filename or f"boleto_{i}.pdf", f.read()) for i, f in enumerate(boletos_files)]

    session_id = uuid.uuid4().hex
    sessao_dir = SESSIONS_DIR / session_id

    try:
        resultado = matcher.conciliar(comprovantes_pdf, boletos, sessao_dir)
    except Exception as e:
        log.exception("Erro ao conciliar")
        # limpa diretorio parcial
        if sessao_dir.exists():
            shutil.rmtree(sessao_dir, ignore_errors=True)
        return jsonify(erro=f"Falha ao processar: {e}"), 500

    # Grava resultado serializado para uso posterior em /download
    (sessao_dir / "_resultado.json").write_text(
        json.dumps(resultado.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    payload = resultado.to_dict()
    payload["session_id"] = session_id
    return jsonify(payload)


@app.route("/preview/<session_id>/<path:filename>")
def preview(session_id: str, filename: str):
    sessao = _sessao_dir(session_id)
    # send_from_directory ja bloqueia path traversal
    return send_from_directory(sessao, filename, mimetype="application/pdf")


@app.route("/download/<session_id>")
def download(session_id: str):
    sessao = _sessao_dir(session_id)
    resultado_path = sessao / "_resultado.json"
    if not resultado_path.exists():
        abort(404, "resultado da sessao nao encontrado")

    data = json.loads(resultado_path.read_text(encoding="utf-8"))
    # Reconstroi resultado minimo so com o que montar_zip precisa
    resultado = matcher.ResultadoConciliacao(
        total_comprovantes=data.get("total_comprovantes", 0),
        comprovantes_orfaos=data.get("comprovantes_orfaos", []),
    )
    for b in data["boletos"]:
        resultado.boletos.append(matcher.ResultadoBoleto(**b))

    zip_bytes = matcher.montar_zip(sessao, resultado)
    return send_file(
        io.BytesIO(zip_bytes),
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"conciliacao_{session_id[:8]}.zip",
    )


@app.route("/health")
def health():
    return {"ok": True}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "9000"))
    # Apenas para desenvolvimento. Em producao use gunicorn (ver README).
    app.run(host="0.0.0.0", port=port, debug=False)
