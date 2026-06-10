"""Cliente OpenAI: extrai linha digitavel/codigo de barras de PDFs.

Como a OpenAI Chat Completions nao aceita PDF como input, convertemos cada
pagina do PDF em imagem PNG via PyMuPDF antes de enviar.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

import pymupdf
from openai import OpenAI

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
        """Linha digitavel normalizada (so digitos) - usada como chave de match."""
        if not self.linha_digitavel:
            return None
        digitos = "".join(_LINHA_DIGITAVEL_RE.findall(self.linha_digitavel))
        if len(digitos) in (47, 48, 44):
            return digitos
        return digitos if digitos else None


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
    return OpenAI(api_key=api_key)


def _modelo() -> str:
    return os.environ.get("OPENAI_MODEL", "gpt-4o-mini")


def _pdf_para_imagens_b64(pdf_bytes: bytes, dpi: int = 150) -> list[str]:
    """Converte cada pagina do PDF em PNG base64 data URL pronto pra OpenAI Vision."""
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

    content: list[dict] = [{"type": "text", "text": prompt}]
    for img_data_url in imagens:
        content.append({
            "type": "image_url",
            "image_url": {"url": img_data_url, "detail": "high"},
        })

    resp = client.chat.completions.create(
        model=_modelo(),
        messages=[{"role": "user", "content": content}],
        response_format={"type": "json_object"},
        temperature=0.0,
        max_tokens=500,
    )

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
