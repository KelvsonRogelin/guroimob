import re
import math
import logging
import pandas as pd
from typing import Optional, Union

# Configuração básica de log (pode ser sobrescrita pelo app principal)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# --------------------------- #
# Utilidades de parsing/limpeza
# --------------------------- #

def _normalize_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()

def _has_text(x) -> bool:
    return isinstance(x, str) and _normalize_spaces(x) != ""

def br_number_to_float(txt: str) -> Optional[float]:
    """
    Converte números em PT-BR/variações para float.
    Ex.: "1.234,56" -> 1234.56 ; "1,234.56" -> 1234.56 ; "189,17" -> 189.17
    """
    if not _has_text(txt):
        return None
    s = txt.strip()

    # mantém apenas dígitos, ponto, vírgula
    s = re.sub(r"[^\d\.,]", "", s)

    if s.count(".") > 0 and s.count(",") > 0:
        # padrão BR típico: 1.234.567,89
        s = s.replace(".", "")
        s = s.replace(",", ".")
        try:
            return float(s)
        except:
            return None

    if "," in s and "." not in s:
        # apenas vírgula => decimal
        s = s.replace(",", ".")
        try:
            return float(s)
        except:
            return None

    if "." in s and "," not in s:
        # apenas ponto: decidir se é decimal ou milhar
        # heurística: se a parte após o último ponto tiver 1-2 dígitos => decimal
        last = s.rsplit(".", 1)
        if len(last[-1]) in (1, 2):
            try:
                return float(s)
            except:
                return None
        else:
            # provavelmente pontos de milhar
            s = s.replace(".", "")
            try:
                return float(s)
            except:
                return None

    # só dígitos
    try:
        return float(s) if s else None
    except:
        return None

def parse_brl_price(preco_raw: str) -> Optional[float]:
    """
    Converte strings de preço em BRL para float (em reais).
    """
    if not _has_text(preco_raw):
        return None

    s = preco_raw.lower()
    s = _normalize_spaces(s)
    s = s.replace("r$", "").replace("reais", "")
    s = s.replace("á vista", "").replace("a vista", "")
    s = s.strip()

    # milhões
    m_mi = re.search(r"([\d\.,]+)\s*(mi|mio|milh(?:ão|oes|ao|oes))", s)
    if m_mi:
        num = br_number_to_float(m_mi.group(1))
        return num * 1_000_000 if num is not None else None

    # mil
    m_mil = re.search(r"([\d\.,]+)\s*mil\b", s)
    if m_mil:
        num = br_number_to_float(m_mil.group(1))
        return num * 1_000 if num is not None else None

    # número "cru" com pontuação BR/EN
    num = br_number_to_float(s)
    return num

def parse_area(area_raw: str, strict: bool = False) -> Optional[float]:
    """
    Extrai área em m².
    Se strict=True, exige que o número esteja seguido de 'm' ou 'm²'.
    """
    if not _has_text(area_raw):
        return None

    s = _normalize_spaces(area_raw).lower()
    
    if strict:
        # Procura padrão número + m/m²
        # Ex: "100m", "100 m²", "100 m2"
        # Regex: numero + espaco opcional + m
        matches = re.findall(r"(\d+(?:[.,]\d+)?)\s*m", s)
        if not matches:
            return None
        nums = matches
    else:
        # coleta TODOS os números (com vírgula/ponto)
        nums = re.findall(r"(\d+(?:[.,]\d+)?)", s)

    if not nums:
        return None

    # converte cada token para float com nossa função robusta
    vals = [br_number_to_float(x) for x in nums]
    vals = [v for v in vals if v is not None]

    if not vals:
        return None

    # se vier faixa "184-190", valores = [184, 190] -> usar o MAIOR (mais conservador p/ R$/m²)
    return float(max(vals))

def fix_titulo(titulo: str, localizacao: str, rua: str) -> str:
    """
    Mantém título quando existir; caso contrário, cria um fallback amigável.
    """
    if _has_text(titulo):
        return _normalize_spaces(titulo)

    loc = _normalize_spaces(localizacao) if _has_text(localizacao) else ""
    r = _normalize_spaces(rua) if _has_text(rua) else ""

    base = loc if loc else "Imóvel"
    if r:
        return f"{base} - {r}"
    return base

def _sane_area_row(row):
    """
    Helper para corrigir área usando outros campos se necessário.
    """
    area = row["_area_num"]
    # Se área for None ou muito pequena (<10) ou muito grande (>10000), tenta re-extrair do texto completo
    if area is None or area < 10 or area > 10000:
        # tenta extrair de algum outro campo textual
        texto = " ".join([str(x) for x in [row.get("localizacao"), row.get("titulo"), row.get("rua")] if _has_text(str(x))])
        # Aqui usamos strict=True para não pegar número da rua
        alt = parse_area(texto, strict=True)
        return alt if alt is not None else area
    return area

# --------------------------- #
# Função principal de filtro
# --------------------------- #

def filtrar_por_m2(
    csv_entrada="imoveis_zap.csv",
    csv_saida="imoveis_filtrados.csv",
    valor_maximo_m2: Optional[float] = None,
) -> int:
    logger.info(f"Lendo {csv_entrada} ...")
    
    try:
        df = pd.read_csv(csv_entrada)
    except FileNotFoundError:
        logger.error(f"Arquivo não encontrado: {csv_entrada}")
        return 0
    except Exception as e:
        logger.error(f"Erro ao ler CSV: {e}")
        return 0

    if df.empty:
        logger.warning("CSV de entrada está vazio.")
        return 0

    # Garantir colunas esperadas
    cols_check = ["preco", "area", "titulo", "localizacao", "rua", "url"]
    for col in cols_check:
        if col not in df.columns:
            df[col] = None

    # Converte preços/áreas de forma robusta
    df["_preco_num"] = df["preco"].apply(parse_brl_price)
    df["_area_num"] = df["area"].apply(parse_area)

    # Aplica correção de área
    df["_area_num"] = df.apply(_sane_area_row, axis=1)

    # drop linhas sem preço ou sem área válida
    before = len(df)
    df = df.dropna(subset=["_preco_num", "_area_num"]).copy()
    df = df[df["_area_num"] > 0]
    after = len(df)
    logger.info(f"Linhas totais: {before} | Válidas para cálculo: {after}")

    if after == 0:
        logger.warning("Nada para calcular. Verifique os campos 'preco' e 'area' no CSV de entrada.")
        return 0

    # valor/m²
    df["valor_m2"] = (df["_preco_num"] / df["_area_num"]).round(2)

    # título fallback
    df["titulo"] = df.apply(lambda r: fix_titulo(r.get("titulo"), r.get("localizacao"), r.get("rua")), axis=1)

    # ordenação por melhor preço por m²
    df = df.sort_values(by="valor_m2", ascending=True)

    # filtro por teto de R$/m² (se informado)
    if valor_maximo_m2 is not None:
        logger.info(f"Aplicando filtro: imóveis com valor_m2 <= {valor_maximo_m2:,.2f}")
        df = df[df["valor_m2"] <= float(valor_maximo_m2)]

    if len(df) == 0:
        logger.warning("Nenhum imóvel dentro do critério definido.")
        return 0

    # seleciona colunas úteis pro output
    cols = [c for c in (
        "titulo url localizacao rua preco condominio area quartos banheiros vagas valor_m2 imagens"
    ).split() if c in df.columns]
    if "valor_m2" not in cols:
        cols.append("valor_m2")

    # salva CSV
    try:
        df.to_csv(csv_saida, index=False, encoding="utf-8-sig", columns=cols)
        logger.info(f"Salvo: {csv_saida} ({len(df)} imóveis)")
        # Exemplo dos melhores
        logger.debug(f"Top 5:\n{df[cols].head(5).to_string(index=False)}")
    except Exception as e:
        logger.error(f"Erro ao salvar CSV de saída: {e}")
        return 0

    return len(df)


if __name__ == "__main__":
    try:
        valor_raw = input(">> Valor máximo de R$/m² (vazio = sem filtro): ").strip()
        valor = float(valor_raw.replace(",", ".")) if valor_raw else None
    except:
        valor = None

    filtrar_por_m2(
        csv_entrada="imoveis_zap.csv",
        csv_saida="imoveis_filtrados.csv",
        valor_maximo_m2=valor,
    )
