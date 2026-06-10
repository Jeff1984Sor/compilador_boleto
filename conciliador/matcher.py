"""Logica de conciliacao: casa boletos com comprovantes em 3 rounds.

Round 1: linha digitavel (alta confianca)
Round 2: valor + beneficiario similar (media confianca)
Round 3: somente valor, casa apenas se candidato unico (baixa confianca)
"""

from __future__ import annotations

import io
import logging
import os
import re
import unicodedata
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, asdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

from . import openai_client as ai_client
from . import pdf_utils

log = logging.getLogger(__name__)

# Limite de similaridade do nome no round 2
_SIMILARIDADE_MIN = 0.6

# Palavras descartadas ao comparar nomes de empresas (alta variabilidade entre boleto e comprovante)
_STOPWORDS_EMPRESA = {
    "LTDA", "LIMITADA", "ME", "EPP", "EIRELI", "MEI",
    "SA", "S/A", "S.A", "S.A.",
    "SOCIEDADE", "EMPRESARIAL",
    "DE", "DA", "DO", "DAS", "DOS", "E",
}


@dataclass
class ResultadoBoleto:
    nome_arquivo: str                       # nome final no ZIP
    boleto_origem: str                      # nome original do upload
    casado: bool
    chave: Optional[str] = None
    linha_digitavel: Optional[str] = None
    valor_boleto: Optional[str] = None
    vencimento: Optional[str] = None
    beneficiario: Optional[str] = None
    comprovante_pagina: Optional[int] = None     # 1-indexed
    pdf_relativo: Optional[str] = None
    erro: Optional[str] = None
    casamento_metodo: Optional[str] = None       # "linha_digitavel" | "valor_beneficiario" | "valor"


@dataclass
class ResultadoConciliacao:
    boletos: list[ResultadoBoleto] = field(default_factory=list)
    comprovantes_orfaos: list[int] = field(default_factory=list)
    total_comprovantes: int = 0

    @property
    def casados(self) -> int:
        return sum(1 for b in self.boletos if b.casado)

    @property
    def total_boletos(self) -> int:
        return len(self.boletos)

    def to_dict(self) -> dict:
        return {
            "boletos": [asdict(b) for b in self.boletos],
            "comprovantes_orfaos": self.comprovantes_orfaos,
            "total_comprovantes": self.total_comprovantes,
            "total_boletos": self.total_boletos,
            "casados": self.casados,
        }


# ---------- helpers de normalizacao ----------

def _normalizar_valor(valor: Optional[str]) -> Optional[int]:
    """Converte uma string monetaria em centavos (int). Aceita 'R$ 1.234,56', '1234.56', etc."""
    if not valor:
        return None
    s = re.sub(r"[^\d,.\-]", "", str(valor))
    if not s:
        return None
    # Detecta formato BR (1.234,56) vs US (1,234.56)
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return int(round(float(s) * 100))
    except (ValueError, OverflowError):
        return None


def _normalizar_nome(nome: Optional[str]) -> str:
    """Uppercase, sem acentos, sem pontuacao, sem stopwords e tokens curtos."""
    if not nome:
        return ""
    s = nome.upper()
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^\w\s]", " ", s)
    tokens = [t for t in s.split() if len(t) > 1 and t not in _STOPWORDS_EMPRESA]
    return " ".join(tokens)


def _similaridade_nomes(a: Optional[str], b: Optional[str]) -> float:
    """Similaridade 0..1 combinando Jaccard de tokens + SequenceMatcher."""
    na, nb = _normalizar_nome(a), _normalizar_nome(b)
    if not na or not nb:
        return 0.0
    tokens_a, tokens_b = set(na.split()), set(nb.split())
    jaccard = len(tokens_a & tokens_b) / len(tokens_a | tokens_b) if tokens_a and tokens_b else 0.0
    seq = SequenceMatcher(None, na, nb).ratio()
    return max(jaccard, seq)


def _processar_paralelo(items: list, func, max_workers: Optional[int] = None):
    # Default conservador (2) para nao estourar TPM da OpenAI em contas Tier 1.
    # Sobrescrivivel via env AI_MAX_WORKERS quando seu tier for maior.
    if max_workers is None:
        max_workers = int(os.environ.get("AI_MAX_WORKERS", "2"))
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        return list(ex.map(func, items))


def _sanitizar_nome_pdf(nome: str) -> str:
    base = os.path.basename(nome).strip()
    if not base.lower().endswith(".pdf"):
        base += ".pdf"
    return base.replace("\\", "_").replace("/", "_")


# ---------- pipeline principal ----------

def conciliar(
    comprovantes_pdf: bytes,
    boletos: list[tuple[str, bytes]],
    sessao_dir: Path,
) -> ResultadoConciliacao:
    """Roda 3 rounds de casamento e grava os PDFs resultantes em `sessao_dir`."""
    sessao_dir.mkdir(parents=True, exist_ok=True)
    pasta_sem = sessao_dir / "sem_comprovante"

    log.info("Iniciando conciliacao: %d boletos", len(boletos))

    # 1. Split de comprovantes
    paginas_comprovantes = pdf_utils.split_por_pagina(comprovantes_pdf)
    log.info("Comprovantes: %d paginas detectadas", len(paginas_comprovantes))

    # 2. Extracao em paralelo
    dados_comprovantes = _processar_paralelo(paginas_comprovantes, ai_client.extrair_dados_comprovante)
    dados_boletos = _processar_paralelo([b[1] for b in boletos], ai_client.extrair_dados_boleto)

    # 3. Inicializa resultado
    resultado = ResultadoConciliacao(total_comprovantes=len(paginas_comprovantes))
    for (nome, _), dados in zip(boletos, dados_boletos):
        resultado.boletos.append(ResultadoBoleto(
            nome_arquivo=_sanitizar_nome_pdf(nome),
            boleto_origem=nome,
            casado=False,
            chave=dados.chave,
            linha_digitavel=dados.linha_digitavel,
            valor_boleto=dados.valor,
            vencimento=dados.vencimento,
            beneficiario=dados.beneficiario,
        ))

    paginas_usadas: set[int] = set()
    boleto_para_pagina: dict[int, int] = {}

    # ---------- ROUND 1: linha digitavel ----------
    indice_chave: dict[str, int] = {}
    for idx, d in enumerate(dados_comprovantes):
        if d.chave:
            indice_chave.setdefault(d.chave, idx)

    for i, r in enumerate(resultado.boletos):
        db = dados_boletos[i]
        if not db.chave or db.chave not in indice_chave:
            continue
        idx_pag = indice_chave[db.chave]
        if idx_pag in paginas_usadas:
            continue
        r.casado = True
        r.comprovante_pagina = idx_pag + 1
        r.casamento_metodo = "linha_digitavel"
        paginas_usadas.add(idx_pag)
        boleto_para_pagina[i] = idx_pag

    log.info("Round 1 (linha digitavel): %d casados", sum(1 for r in resultado.boletos if r.casado))

    # ---------- ROUND 2: valor + beneficiario ----------
    for i, r in enumerate(resultado.boletos):
        if r.casado:
            continue
        db = dados_boletos[i]
        valor_boleto = _normalizar_valor(db.valor)
        if valor_boleto is None:
            continue

        candidatos: list[tuple[int, float]] = []
        for idx_pag in range(len(paginas_comprovantes)):
            if idx_pag in paginas_usadas:
                continue
            dc = dados_comprovantes[idx_pag]
            if _normalizar_valor(dc.valor) != valor_boleto:
                continue
            sim = _similaridade_nomes(db.beneficiario, dc.beneficiario)
            if sim >= _SIMILARIDADE_MIN:
                candidatos.append((idx_pag, sim))

        if candidatos:
            candidatos.sort(key=lambda x: x[1], reverse=True)
            idx_pag, _ = candidatos[0]
            r.casado = True
            r.comprovante_pagina = idx_pag + 1
            r.casamento_metodo = "valor_beneficiario"
            paginas_usadas.add(idx_pag)
            boleto_para_pagina[i] = idx_pag

    log.info("Apos Round 2 (valor+nome): %d casados", sum(1 for r in resultado.boletos if r.casado))

    # ---------- ROUND 3: somente valor (apenas se candidato unico) ----------
    for i, r in enumerate(resultado.boletos):
        if r.casado:
            continue
        db = dados_boletos[i]
        valor_boleto = _normalizar_valor(db.valor)
        if valor_boleto is None:
            continue

        candidatos = [
            idx_pag for idx_pag in range(len(paginas_comprovantes))
            if idx_pag not in paginas_usadas
            and _normalizar_valor(dados_comprovantes[idx_pag].valor) == valor_boleto
        ]

        if len(candidatos) == 1:
            idx_pag = candidatos[0]
            r.casado = True
            r.comprovante_pagina = idx_pag + 1
            r.casamento_metodo = "valor"
            paginas_usadas.add(idx_pag)
            boleto_para_pagina[i] = idx_pag
        elif len(candidatos) > 1:
            r.erro = f"Ambiguo: {len(candidatos)} comprovantes com valor R$ {db.valor or '?'} (paginas {[c+1 for c in candidatos]})"

    log.info("Apos Round 3 (so valor): %d casados", sum(1 for r in resultado.boletos if r.casado))

    # ---------- ROUND 4: pareamento de duplicatas ----------
    # Caso comum: 2 boletos com mesmo valor+pagador e 2 comprovantes PIX correspondentes.
    # Round 2/3 acima nao casam corretamente porque ha multiplos candidatos identicos.
    # Aqui: para cada valor onde sobraram N boletos sem match e N comprovantes orfaos
    # (mesma quantidade), parear na ordem em que foram enviados. Nao chama Gemini.
    boletos_restantes_por_valor: dict[int, list[int]] = {}
    for i, r in enumerate(resultado.boletos):
        if r.casado:
            continue
        v = _normalizar_valor(dados_boletos[i].valor)
        if v is not None:
            boletos_restantes_por_valor.setdefault(v, []).append(i)

    comprovantes_restantes_por_valor: dict[int, list[int]] = {}
    for idx_pag in range(len(paginas_comprovantes)):
        if idx_pag in paginas_usadas:
            continue
        v = _normalizar_valor(dados_comprovantes[idx_pag].valor)
        if v is not None:
            comprovantes_restantes_por_valor.setdefault(v, []).append(idx_pag)

    for valor_cent, idxs_boletos in boletos_restantes_por_valor.items():
        idxs_comprovantes = comprovantes_restantes_por_valor.get(valor_cent, [])
        if len(idxs_boletos) >= 2 and len(idxs_boletos) == len(idxs_comprovantes):
            for idx_b, idx_p in zip(idxs_boletos, idxs_comprovantes):
                r = resultado.boletos[idx_b]
                r.casado = True
                r.comprovante_pagina = idx_p + 1
                r.casamento_metodo = "pares_duplicados"
                r.erro = None  # limpa a marcacao "ambiguo" deixada pelo round 3
                paginas_usadas.add(idx_p)
                boleto_para_pagina[idx_b] = idx_p
            log.info("Round 4: pareou %d duplicatas com valor %d centavos", len(idxs_boletos), valor_cent)

    log.info("Apos Round 4 (pares duplicados): %d casados", sum(1 for r in resultado.boletos if r.casado))

    # ---------- Grava PDFs e finaliza relatorio ----------
    for i, r in enumerate(resultado.boletos):
        nome, boleto_bytes = boletos[i]
        if r.casado:
            idx_pag = boleto_para_pagina[i]
            pdf_merged = pdf_utils.merge_pdfs([boleto_bytes, paginas_comprovantes[idx_pag]])
            dest = sessao_dir / r.nome_arquivo
            dest.write_bytes(pdf_merged)
            r.pdf_relativo = r.nome_arquivo
        else:
            pasta_sem.mkdir(parents=True, exist_ok=True)
            dest = pasta_sem / r.nome_arquivo
            dest.write_bytes(boleto_bytes)
            r.pdf_relativo = f"sem_comprovante/{r.nome_arquivo}"
            if not r.erro:
                if not dados_boletos[i].chave and _normalizar_valor(dados_boletos[i].valor) is None:
                    r.erro = "Nao foi possivel ler dados do boleto (linha digitavel e valor)"
                else:
                    r.erro = "Nenhum comprovante correspondente encontrado"

    resultado.comprovantes_orfaos = sorted(
        i + 1 for i in range(len(paginas_comprovantes)) if i not in paginas_usadas
    )

    log.info("Conciliacao concluida: %d/%d casados", resultado.casados, resultado.total_boletos)
    return resultado


# ---------- relatorio e ZIP ----------

_NOMES_METODO = {
    "linha_digitavel": "linha digitavel",
    "valor_beneficiario": "valor + nome",
    "valor": "so valor",
    "pares_duplicados": "pares duplicados",
}


def montar_zip(sessao_dir: Path, resultado: ResultadoConciliacao) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for boleto in resultado.boletos:
            if not boleto.pdf_relativo:
                continue
            caminho = sessao_dir / boleto.pdf_relativo
            if caminho.exists():
                zf.write(caminho, arcname=boleto.pdf_relativo)
        zf.writestr("_conciliacao.txt", _gerar_relatorio(resultado))
    return buf.getvalue()


def _gerar_relatorio(r: ResultadoConciliacao) -> str:
    por_metodo: dict[str, int] = {}
    for b in r.boletos:
        if b.casado and b.casamento_metodo:
            por_metodo[b.casamento_metodo] = por_metodo.get(b.casamento_metodo, 0) + 1

    linhas = [
        "=== Relatorio de Conciliacao ===",
        f"Total de boletos: {r.total_boletos}",
        f"Total de comprovantes (paginas): {r.total_comprovantes}",
        f"Boletos casados: {r.casados}",
    ]
    for m, n in por_metodo.items():
        linhas.append(f"  - {_NOMES_METODO.get(m, m)}: {n}")
    linhas.append(f"Boletos sem comprovante: {r.total_boletos - r.casados}")
    linhas.append(f"Comprovantes orfaos (paginas): {r.comprovantes_orfaos or 'nenhum'}")
    linhas.append("")
    linhas.append("--- Detalhe por boleto ---")
    for b in r.boletos:
        status = "OK" if b.casado else "SEM COMPROVANTE"
        metodo = f" via {_NOMES_METODO.get(b.casamento_metodo, b.casamento_metodo)}" if b.casamento_metodo else ""
        pag = f" pag.{b.comprovante_pagina}" if b.comprovante_pagina else ""
        erro = f" - {b.erro}" if b.erro else ""
        linhas.append(f"[{status}]{pag}{metodo} {b.nome_arquivo}{erro}")
    return "\n".join(linhas) + "\n"
