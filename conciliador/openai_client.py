"""Cliente OpenAI: extrai linha digitavel/codigo de barras de PDFs.

Como a OpenAI Chat Completions nao aceita PDF como input, convertemos cada
pagina do PDF em imagem PNG via PyMuPDF antes de enviar.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import random
import re
import threading
import time
from dataclasses import dataclass
from typing import Optional

import pymupdf
from openai import OpenAI, RateLimitError

from .chave import canonica as _chave_canonica

log = logging.getLogger(__name__)

_LINHA_DIGITAVEL_RE = re.compile(r"\d")


@dataclass
class DadosTitulo:
    """Dados extraidos de um boleto ou comprovante."""
    linha_digitavel: Optional[str]
    valor: Optional[str] = None
    vencimento: Optional[str] = None
    beneficiario: Optional[str] = None
    raw: Optional[str] = None

    @property
    def chave(self) -> Optional[str]:
        """Codigo de barras canonico (44 dig) - usado como chave de match.

        Normaliza linha digitavel (47/48) e codigo de barras (44) para a mesma
        representacao, para casar boleto x comprovante mesmo em formatos diferentes.
        """
        return _chave_canonica(self.linha_digitavel)


_PROMPT_BOLETO = """\
Voce esta lendo um BOLETO BANCARIO brasileiro.
Extraia os campos abaixo e devolva APENAS um JSON valido (sem markdown, sem texto antes ou depois).

Campos:
- linha_digitavel: a linha digitavel completa do boleto (47 ou 48 digitos, pode vir formatada com pontos e espacos - devolva EXATAMENTE como aparece)
- valor: valor do documento (ex.: "1.234,56")
- vencimento: data de vencimento no formato dd/mm/aaaa
- beneficiario: nome do beneficiario / cedente

Se algum campo nao for encontrado, use null.

Exemplo de saida:
{"linha_digitavel": "23793.39001 60000.000000 00000.000000 1 12345678901234", "valor": "1.234,56", "vencimento": "15/06/2026", "beneficiario": "EMPRESA XYZ LTDA"}
"""

_PROMPT_COMPROVANTE = """\
Voce esta lendo um COMPROVANTE DE PAGAMENTO brasileiro (boleto bancario OU PIX).
Extraia os campos abaixo e devolva APENAS um JSON valido (sem markdown, sem texto antes ou depois).

Campos:
- linha_digitavel: linha digitavel do boleto pago (47/48 digitos). Se for PIX ou nao tiver linha digitavel visivel, use null.
- valor: valor pago (ex.: "1.234,56")
- vencimento: data de vencimento ou pagamento (dd/mm/aaaa)
- beneficiario: nome do beneficiario / favorecido (quem recebeu)

Se algum campo nao for encontrado, use null.

Exemplo de saida:
{"linha_digitavel": null, "valor": "1.234,56", "vencimento": "15/06/2026", "beneficiario": "EMPRESA XYZ LTDA"}
"""


def _client() -> OpenAI:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY nao definida no ambiente (.env)")
    # max_retries: SDK reenfileira automaticamente em 429 (TPM) e 5xx,
    # com backoff exponencial. Importante em contas Tier 1.
    return OpenAI(api_key=api_key, max_retries=6, timeout=120.0)


def _modelo() -> str:
    return os.environ.get("OPENAI_MODEL", "gpt-4o-mini")


def _pdf_para_imagens_b64(pdf_bytes: bytes) -> list[str]:
    """Converte cada pagina do PDF em PNG base64 data URL pronto pra OpenAI Vision.

    DPI configuravel via env OPENAI_PDF_DPI (default 120).
    """
    dpi = int(os.environ.get("OPENAI_PDF_DPI", "120"))
    imagens: list[str] = []
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    try:
        for page in doc:
            pix = page.get_pixmap(dpi=dpi)
            png_bytes = pix.tobytes("png")
            b64 = base64.b64encode(png_bytes).decode("ascii")
            imagens.append(f"data:image/png;base64,{b64}")
    finally:
        doc.close()
    return imagens


def _detail_imagem() -> str:
    """Detail level enviado pro OpenAI Vision. 'low' = ~85 tokens por imagem;
    'high' = ate ~25k tokens. Default 'low' pra caber no Tier 1 (200k TPM).
    Override via env OPENAI_IMAGE_DETAIL=high se sua conta tem mais TPM."""
    return os.environ.get("OPENAI_IMAGE_DETAIL", "low")


# Lock global para serializar chamadas durante Tier 1 (workaround do TPM apertado).
# Quando subir de tier, deixa OPENAI_SERIALIZE=0 no .env (ou remove a variavel).
_LOCK_SERIALIZACAO = threading.Lock()


def _deve_serializar() -> bool:
    """Quando true, garante 1 chamada por vez (uso correto em Tier 1 OpenAI)."""
    return os.environ.get("OPENAI_SERIALIZE", "1") != "0"


def _max_retries_429() -> int:
    return int(os.environ.get("OPENAI_MAX_429_RETRIES", "10"))


def _chamar_com_backoff(client: OpenAI, **kwargs):
    """Faz a call ao chat.completions com retry exponencial agressivo em 429.

    O SDK ja faz 6 retries automaticos, mas com timeout curto. Aqui aumentamos
    e respeitamos o header 'retry-after' do OpenAI quando ele indica espera.
    """
    max_tentativas = _max_retries_429()
    for tentativa in range(max_tentativas):
        try:
            return client.chat.completions.create(**kwargs)
        except RateLimitError as e:
            # Tenta extrair "try again in Xs" da mensagem
            espera = _parse_retry_after(e)
            if espera is None:
                # Backoff exponencial com jitter: 2, 4, 8, 16, 32, 64...
                espera = min(60, 2 ** (tentativa + 1)) + random.uniform(0, 1)
            if tentativa == max_tentativas - 1:
                log.error("Esgotaram %d retries em 429. Desistindo.", max_tentativas)
                raise
            log.warning(
                "429 (tentativa %d/%d). Aguardando %.1fs antes de retentar.",
                tentativa + 1, max_tentativas, espera,
            )
            time.sleep(espera)


_RETRY_AFTER_RE = re.compile(r"try again in ([\d.]+)(ms|s)", re.IGNORECASE)


def _parse_retry_after(err: Exception) -> Optional[float]:
    msg = str(err)
    m = _RETRY_AFTER_RE.search(msg)
    if not m:
        return None
    valor = float(m.group(1))
    unidade = m.group(2).lower()
    return valor / 1000.0 if unidade == "ms" else valor


def _parse_json(texto: str) -> dict:
    """Tolera ```json ... ``` fences que alguns modelos ainda devolvem."""
    t = texto.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    return json.loads(t)


def _extrair(pdf_bytes: bytes, prompt: str) -> DadosTitulo:
    client = _client()
    imagens = _pdf_para_imagens_b64(pdf_bytes)

    detail = _detail_imagem()
    content: list[dict] = [{"type": "text", "text": prompt}]
    for img_data_url in imagens:
        content.append({
            "type": "image_url",
            "image_url": {"url": img_data_url, "detail": detail},
        })

    # Em Tier 1 do OpenAI o TPM eh apertado: serializa as chamadas para evitar
    # burst de tokens e usa backoff exponencial respeitando o retry-after.
    kwargs = dict(
        model=_modelo(),
        messages=[{"role": "user", "content": content}],
        response_format={"type": "json_object"},
        temperature=0.0,
        max_tokens=500,
    )
    if _deve_serializar():
        with _LOCK_SERIALIZACAO:
            resp = _chamar_com_backoff(client, **kwargs)
    else:
        resp = _chamar_com_backoff(client, **kwargs)

    texto = (resp.choices[0].message.content or "").strip()
    try:
        data = _parse_json(texto)
    except json.JSONDecodeError:
        log.warning("Resposta OpenAI nao-JSON: %s", texto[:200])
        return DadosTitulo(linha_digitavel=None, raw=texto)

    if isinstance(data, list):
        if not data:
            return DadosTitulo(linha_digitavel=None, raw=texto)
        if len(data) > 1:
            log.warning("OpenAI retornou %d itens; usando o primeiro.", len(data))
        data = data[0]

    if not isinstance(data, dict):
        log.warning("Resposta OpenAI com tipo inesperado (%s): %s", type(data).__name__, texto[:200])
        return DadosTitulo(linha_digitavel=None, raw=texto)

    return DadosTitulo(
        linha_digitavel=data.get("linha_digitavel"),
        valor=data.get("valor"),
        vencimento=data.get("vencimento"),
        beneficiario=data.get("beneficiario"),
        raw=texto,
    )


def extrair_dados_boleto(pdf_bytes: bytes) -> DadosTitulo:
    """Extrai linha digitavel e metadados de um PDF de boleto."""
    return _extrair(pdf_bytes, _PROMPT_BOLETO)


def extrair_dados_comprovante(pdf_bytes: bytes) -> DadosTitulo:
    """Extrai linha digitavel e metadados de uma pagina de comprovante."""
    return _extrair(pdf_bytes, _PROMPT_COMPROVANTE)
