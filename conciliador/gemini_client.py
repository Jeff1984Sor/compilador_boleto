"""Cliente Gemini: extrai linha digitavel/codigo de barras de PDFs de boletos e comprovantes."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

from google import genai
from google.genai import types

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
        """Linha digitavel normalizada (so digitos) — usada como chave de match."""
        if not self.linha_digitavel:
            return None
        digitos = "".join(_LINHA_DIGITAVEL_RE.findall(self.linha_digitavel))
        # Linha digitavel tem 47 (boleto bancario) ou 48 (arrecadacao) digitos
        if len(digitos) in (47, 48):
            return digitos
        # Codigo de barras (44 digitos) tambem serve como chave
        if len(digitos) == 44:
            return digitos
        return digitos if digitos else None


_PROMPT_BOLETO = """\
Voce esta lendo um BOLETO BANCARIO brasileiro em PDF.
Extraia os campos abaixo e devolva APENAS um JSON valido (sem markdown, sem texto antes ou depois).

Campos:
- linha_digitavel: a linha digitavel completa do boleto (47 ou 48 digitos, pode vir formatada com pontos e espacos — devolva EXATAMENTE como aparece)
- valor: valor do documento (ex.: "1.234,56")
- vencimento: data de vencimento no formato dd/mm/aaaa
- beneficiario: nome do beneficiario / cedente

Se algum campo nao for encontrado, use null.

Exemplo de saida:
{"linha_digitavel": "23793.39001 60000.000000 00000.000000 1 12345678901234", "valor": "1.234,56", "vencimento": "15/06/2026", "beneficiario": "EMPRESA XYZ LTDA"}
"""

_PROMPT_COMPROVANTE = """\
Voce esta lendo um COMPROVANTE DE PAGAMENTO de boleto bancario brasileiro em PDF (1 pagina).
Extraia os campos abaixo e devolva APENAS um JSON valido (sem markdown, sem texto antes ou depois).

Campos:
- linha_digitavel: a linha digitavel do boleto pago (47 ou 48 digitos — devolva EXATAMENTE como aparece, com pontuacao se houver). Se so houver codigo de barras (44 digitos), retorne ele.
- valor: valor pago (ex.: "1.234,56")
- vencimento: data de vencimento ou pagamento (dd/mm/aaaa)
- beneficiario: nome do beneficiario / favorecido

Se algum campo nao for encontrado, use null.

Exemplo de saida:
{"linha_digitavel": "23793390016000000000000000000011234567890123456", "valor": "1.234,56", "vencimento": "15/06/2026", "beneficiario": "EMPRESA XYZ LTDA"}
"""


def _client() -> genai.Client:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY nao definida no ambiente (.env)")
    return genai.Client(api_key=api_key)


def _modelo() -> str:
    return os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")


def _parse_json(texto: str) -> dict:
    """Tenta extrair JSON do texto retornado pelo modelo (tolera markdown fences)."""
    t = texto.strip()
    if t.startswith("```"):
        # remove fences ```json ... ```
        t = re.sub(r"^```(?:json)?\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    return json.loads(t)


def _extrair(pdf_bytes: bytes, prompt: str) -> DadosTitulo:
    client = _client()
    resp = client.models.generate_content(
        model=_modelo(),
        contents=[
            types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
            prompt,
        ],
        config=types.GenerateContentConfig(
            temperature=0.0,
            response_mime_type="application/json",
        ),
    )
    texto = (resp.text or "").strip()
    try:
        data = _parse_json(texto)
    except json.JSONDecodeError:
        log.warning("Resposta Gemini nao-JSON: %s", texto[:200])
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
