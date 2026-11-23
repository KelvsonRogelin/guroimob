"""Camada de persistência (SQLite) do Guru Imob.

Multiusuário: cada imóvel pertence a um usuário (workspace). A chave de
deduplicação é (usuario_id, url) — a mesma URL pode ser monitorada por vários
usuários sem que um enxergue os dados do outro. O histórico de preços
(precos_hist) e as capturas também são escopados por usuário.
"""
import csv
import io
import json
import logging
import os
import re
import sqlite3
from datetime import datetime

from werkzeug.security import generate_password_hash, check_password_hash

from filter import parse_brl_price, parse_area, fix_titulo

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "imoveis.db")

# colunas exportadas para CSV / Google Sheets (mesmo contrato de antes)
CAMPOS_CSV = [
    "titulo", "url", "localizacao", "rua", "preco", "condominio",
    "area", "quartos", "banheiros", "vagas", "valor_m2", "imagens",
]

# Colunas de `imoveis` (a ordem é usada na migração de bancos antigos).
_COLS_IMOVEIS = [
    "url", "titulo", "localizacao", "rua", "condominio", "area", "area_num",
    "quartos", "quartos_num", "banheiros", "vagas", "imagens", "preco",
    "preco_num", "valor_m2", "primeira_vez", "ultima_vez", "preco_ant",
    "variacao", "ativo",
]

_DDL_IMOVEIS = """
CREATE TABLE IF NOT EXISTS imoveis (
  usuario_id    INTEGER NOT NULL,
  url           TEXT NOT NULL,
  titulo        TEXT,
  localizacao   TEXT,
  rua           TEXT,
  condominio    TEXT,
  area          TEXT,
  area_num      REAL,
  quartos       TEXT,
  quartos_num   INTEGER,
  banheiros     TEXT,
  vagas         TEXT,
  imagens       TEXT,
  preco         TEXT,
  preco_num     REAL,
  valor_m2      REAL,
  primeira_vez  TEXT,
  ultima_vez    TEXT,
  preco_ant     REAL,
  variacao      REAL,
  ativo         INTEGER DEFAULT 1,
  alerta_m2     REAL,
  alerta_disparado INTEGER DEFAULT 0,
  PRIMARY KEY (usuario_id, url)
);
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS usuarios (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  nome        TEXT NOT NULL,
  email       TEXT NOT NULL UNIQUE,
  senha_hash  TEXT NOT NULL,
  whatsapp    TEXT,
  criado_em   TEXT
);
""" + _DDL_IMOVEIS + """
CREATE TABLE IF NOT EXISTS precos_hist (
  usuario_id INTEGER NOT NULL,
  url        TEXT NOT NULL,
  quando     TEXT NOT NULL,
  preco_num  REAL
);
CREATE INDEX IF NOT EXISTS idx_hist_url ON precos_hist(usuario_id, url);
CREATE TABLE IF NOT EXISTS capturas (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  usuario_id  INTEGER,
  quando      TEXT,
  url_busca   TEXT,
  capturados  INTEGER,
  com_preco   INTEGER,
  novos       INTEGER,
  quedas      INTEGER
);
CREATE TABLE IF NOT EXISTS notificacoes (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  usuario_id  INTEGER NOT NULL,
  url         TEXT NOT NULL,
  titulo      TEXT,
  quando      TEXT,
  preco_num   REAL,
  valor_m2    REAL,
  alerta_m2   REAL,
  whatsapp    TEXT,
  enviado     INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_notif_user ON notificacoes(usuario_id);
CREATE TABLE IF NOT EXISTS suporte (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  usuario_id  INTEGER,
  tipo        TEXT,
  assunto     TEXT,
  mensagem    TEXT,
  quando      TEXT,
  enviado     INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_suporte_user ON suporte(usuario_id);
"""

ORDENAVEIS = {
    "valor_m2": "valor_m2",
    "preco": "preco_num",
    "area": "area_num",
    "quartos": "quartos_num",
    "quando": "ultima_vez",
    "titulo": "titulo",
}


# --------------------------------------------------------------------------- #
# Conexão / schema
# --------------------------------------------------------------------------- #

def conectar():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with conectar() as con:
        con.executescript(SCHEMA)


def _colunas(con, tabela):
    try:
        return {r["name"] for r in con.execute(f"PRAGMA table_info({tabela})").fetchall()}
    except sqlite3.Error:
        return set()


def _tabela_existe(con, tabela):
    r = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (tabela,)
    ).fetchone()
    return r is not None


def _garantir_colunas(con, tabela, colunas):
    """Adiciona colunas que ainda não existem (migração leve, sem perder dados)."""
    existentes = _colunas(con, tabela)
    if not existentes:
        return
    for nome, ddl in colunas.items():
        if nome not in existentes:
            con.execute(f"ALTER TABLE {tabela} ADD COLUMN {nome} {ddl}")
            logger.info(f"Coluna '{tabela}.{nome}' adicionada.")


# --------------------------------------------------------------------------- #
# Usuários (workspaces)
# --------------------------------------------------------------------------- #

def _normalizar_email(email):
    return (email or "").strip().lower()


def criar_usuario(nome, email, senha):
    """Cria um usuário (workspace próprio). Retorna o id ou None se o e-mail já existe."""
    agora = datetime.now().isoformat(timespec="seconds")
    con = conectar()
    try:
        cur = con.execute(
            "INSERT INTO usuarios (nome, email, senha_hash, criado_em) VALUES (?, ?, ?, ?)",
            (nome.strip(), _normalizar_email(email), generate_password_hash(senha), agora),
        )
        con.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None
    finally:
        con.close()


def autenticar(email, senha):
    """Valida as credenciais e devolve os dados públicos do usuário (ou None)."""
    con = conectar()
    try:
        u = con.execute(
            "SELECT id, nome, email, senha_hash FROM usuarios WHERE email = ?",
            (_normalizar_email(email),),
        ).fetchone()
    finally:
        con.close()
    if u and check_password_hash(u["senha_hash"], senha or ""):
        return {"id": u["id"], "nome": u["nome"], "email": u["email"]}
    return None


def obter_usuario(usuario_id):
    con = conectar()
    try:
        u = con.execute(
            "SELECT id, nome, email, whatsapp FROM usuarios WHERE id = ?", (usuario_id,)
        ).fetchone()
    finally:
        con.close()
    return dict(u) if u else None


def definir_whatsapp(usuario_id, numero):
    """Grava o número de WhatsApp do usuário (destino das notificações)."""
    numero = (numero or "").strip()
    con = conectar()
    try:
        con.execute("UPDATE usuarios SET whatsapp = ? WHERE id = ?",
                    (numero or None, usuario_id))
        con.commit()
    finally:
        con.close()
    logger.info(f"[usuário {usuario_id}] WhatsApp atualizado.")
    return numero or None


def atualizar_perfil(usuario_id, nome, email):
    """Atualiza nome e e-mail. Retorna None se ok, ou 'email_exists'/'not_found'."""
    con = conectar()
    try:
        try:
            cur = con.execute(
                "UPDATE usuarios SET nome = ?, email = ? WHERE id = ?",
                ((nome or "").strip(), _normalizar_email(email), usuario_id),
            )
        except sqlite3.IntegrityError:
            return "email_exists"
        con.commit()
        if not cur.rowcount:
            return "not_found"
        logger.info(f"[usuário {usuario_id}] Perfil atualizado.")
        return None
    finally:
        con.close()


def alterar_senha(usuario_id, senha_atual, nova_senha):
    """Troca a senha após validar a atual. Retorna True se trocou, False se a atual está errada."""
    con = conectar()
    try:
        u = con.execute("SELECT senha_hash FROM usuarios WHERE id = ?", (usuario_id,)).fetchone()
        if not u or not check_password_hash(u["senha_hash"], senha_atual or ""):
            return False
        con.execute("UPDATE usuarios SET senha_hash = ? WHERE id = ?",
                    (generate_password_hash(nova_senha), usuario_id))
        con.commit()
        logger.info(f"[usuário {usuario_id}] Senha alterada.")
        return True
    finally:
        con.close()


def _garantir_usuario(con, nome, email, senha):
    """Retorna o id do usuário, criando-o se ainda não existir."""
    email = _normalizar_email(email)
    u = con.execute("SELECT id FROM usuarios WHERE email = ?", (email,)).fetchone()
    if u:
        return u["id"]
    cur = con.execute(
        "INSERT INTO usuarios (nome, email, senha_hash, criado_em) VALUES (?, ?, ?, ?)",
        (nome, email, generate_password_hash(senha),
         datetime.now().isoformat(timespec="seconds")),
    )
    logger.info(f"Usuário admin criado: {email}")
    return cur.lastrowid


# --------------------------------------------------------------------------- #
# Migração para multiusuário
# --------------------------------------------------------------------------- #

def migrar_multiusuario(admin_email, admin_senha, admin_nome="Administrador"):
    """Garante o schema multiusuário e preserva os dados dos bancos antigos.

    Bancos criados antes deste recurso têm `imoveis` com PK = url e sem
    `usuario_id`. Aqui a tabela é reconstruída com PK (usuario_id, url) e todos
    os registros existentes são atribuídos à conta admin — nada é perdido.

    Retorna o id do usuário admin.
    """
    con = conectar()
    try:
        admin_id = _garantir_usuario(con, admin_nome, admin_email, admin_senha)

        if _tabela_existe(con, "imoveis") and "usuario_id" not in _colunas(con, "imoveis"):
            _recriar_imoveis(con, admin_id)
        for tabela in ("precos_hist", "capturas"):
            if _tabela_existe(con, tabela) and "usuario_id" not in _colunas(con, tabela):
                con.execute(f"ALTER TABLE {tabela} ADD COLUMN usuario_id INTEGER")
                con.execute(f"UPDATE {tabela} SET usuario_id = ?", (admin_id,))
                logger.info(f"Tabela '{tabela}' migrada para multiusuário.")

        # Colunas de alertas/notificações (compatível com bancos já existentes)
        _garantir_colunas(con, "usuarios", {"whatsapp": "TEXT"})
        _garantir_colunas(con, "imoveis", {
            "alerta_m2": "REAL",
            "alerta_disparado": "INTEGER DEFAULT 0",
        })

        con.commit()
    finally:
        con.close()
    return admin_id


def _recriar_imoveis(con, usuario_id):
    """Reconstrói `imoveis` com PK composta, atribuindo as linhas antigas ao usuário."""
    con.executescript("ALTER TABLE imoveis RENAME TO imoveis_antigo;")
    con.executescript(_DDL_IMOVEIS)
    cols = ", ".join(_COLS_IMOVEIS)
    con.execute(
        f"INSERT INTO imoveis (usuario_id, {cols}) SELECT ?, {cols} FROM imoveis_antigo",
        (usuario_id,),
    )
    con.executescript("DROP TABLE imoveis_antigo;")
    logger.info("Tabela 'imoveis' migrada para multiusuário (PK composta).")


# --------------------------------------------------------------------------- #
# Normalização de valores vindos do scraper / CSV / seed
# --------------------------------------------------------------------------- #

def _texto(v):
    """Normaliza um valor bruto para string limpa ou None."""
    if v is None:
        return None
    s = str(v).strip()
    if s == "" or s.lower() == "nan":
        return None
    return s


def _ultima_linha(v):
    """Campos do tipo 'Tamanho do imóvel\\n59 m²' -> '59 m²'."""
    s = _texto(v)
    if not s:
        return None
    linhas = [l.strip() for l in s.splitlines() if l.strip()]
    return linhas[-1] if linhas else None


def _inteiro(v):
    s = _ultima_linha(v)
    if not s:
        return None
    m = re.search(r"\d+", s)
    return int(m.group()) if m else None


def _imagens(v):
    """Guarda a lista de imagens como JSON (ou o texto original)."""
    if v is None:
        return None
    if isinstance(v, list):
        return json.dumps(v, ensure_ascii=False)
    return _texto(v)


def _preparar(reg):
    """Converte um registro bruto em linha normalizada do banco."""
    url = _texto(reg.get("url"))
    if not url:
        return None

    preco_txt = _texto(reg.get("preco"))
    area_txt = _texto(reg.get("area"))
    preco_num = parse_brl_price(preco_txt) if preco_txt else None
    area_num = parse_area(area_txt) if area_txt else None
    valor_m2 = round(preco_num / area_num, 2) if (preco_num and area_num and area_num > 0) else None

    return {
        "url": url,
        "titulo": fix_titulo(_texto(reg.get("titulo")), _texto(reg.get("localizacao")), _texto(reg.get("rua"))),
        "localizacao": _texto(reg.get("localizacao")),
        "rua": _texto(reg.get("rua")),
        "condominio": _texto(reg.get("condominio")),
        "area": _ultima_linha(area_txt),
        "area_num": area_num,
        "quartos": _ultima_linha(reg.get("quartos")),
        "quartos_num": _inteiro(reg.get("quartos")),
        "banheiros": _ultima_linha(reg.get("banheiros")),
        "vagas": _ultima_linha(reg.get("vagas")),
        "imagens": _imagens(reg.get("imagens")),
        "preco": preco_txt,
        "preco_num": preco_num,
        "valor_m2": valor_m2,
    }


# --------------------------------------------------------------------------- #
# Escrita
# --------------------------------------------------------------------------- #

def salvar_registros(registros, usuario_id, url_busca=None, registrar_captura=True):
    """Insere/atualiza imóveis do usuário com dedup por URL, histórico de preço
    e avaliação dos alertas de R$/m² (gera notificações quando o alvo é atingido).
    """
    agora = datetime.now().isoformat(timespec="seconds")
    novos = quedas = com_preco = processados = 0
    alertas = []

    con = conectar()
    try:
        u = con.execute("SELECT whatsapp FROM usuarios WHERE id = ?", (usuario_id,)).fetchone()
        whatsapp = u["whatsapp"] if u else None

        for reg in registros:
            linha = _preparar(reg)
            if not linha:
                continue
            processados += 1
            if linha["preco_num"] is not None:
                com_preco += 1

            # histórico de preço (escopado por usuário)
            if linha["preco_num"] is not None:
                con.execute(
                    "INSERT INTO precos_hist (usuario_id, url, quando, preco_num) VALUES (?, ?, ?, ?)",
                    (usuario_id, linha["url"], agora, linha["preco_num"]),
                )

            existe = con.execute(
                "SELECT preco_num FROM imoveis WHERE usuario_id = ? AND url = ?",
                (usuario_id, linha["url"]),
            ).fetchone()

            if existe is None:
                novos += 1
                con.execute(
                    """INSERT INTO imoveis
                       (usuario_id, url, titulo, localizacao, rua, condominio, area, area_num,
                        quartos, quartos_num, banheiros, vagas, imagens,
                        preco, preco_num, valor_m2, primeira_vez, ultima_vez,
                        preco_ant, variacao, ativo)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                    (usuario_id, linha["url"], linha["titulo"], linha["localizacao"],
                     linha["rua"], linha["condominio"], linha["area"], linha["area_num"],
                     linha["quartos"], linha["quartos_num"], linha["banheiros"],
                     linha["vagas"], linha["imagens"], linha["preco"], linha["preco_num"],
                     linha["valor_m2"], agora, agora, None, None),
                )
            else:
                ant = existe["preco_num"]
                variacao = None
                if linha["preco_num"] is not None and ant is not None:
                    variacao = round(linha["preco_num"] - ant, 2)
                    if variacao < 0:
                        quedas += 1
                con.execute(
                    """UPDATE imoveis SET
                         titulo=?, localizacao=?, rua=?, condominio=?, area=?, area_num=?,
                         quartos=?, quartos_num=?, banheiros=?, vagas=?, imagens=?,
                         preco=?, preco_num=?, valor_m2=?, ultima_vez=?, preco_ant=?, variacao=?
                       WHERE usuario_id=? AND url=?""",
                    (linha["titulo"], linha["localizacao"], linha["rua"], linha["condominio"],
                     linha["area"], linha["area_num"], linha["quartos"], linha["quartos_num"],
                     linha["banheiros"], linha["vagas"], linha["imagens"], linha["preco"],
                     linha["preco_num"], linha["valor_m2"], agora, ant, variacao,
                     usuario_id, linha["url"]),
                )

            disparo = _avaliar_alerta(con, usuario_id, linha, whatsapp, agora)
            if disparo:
                alertas.append(disparo)

        if registrar_captura:
            con.execute(
                """INSERT INTO capturas (usuario_id, quando, url_busca, capturados, com_preco, novos, quedas)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (usuario_id, agora, url_busca, processados, com_preco, novos, quedas),
            )
        con.commit()
    finally:
        con.close()

    logger.info(
        f"[usuário {usuario_id}] Banco atualizado: {processados} registros | "
        f"novos: {novos} | quedas: {quedas} | alertas: {len(alertas)}"
    )
    return {"total": processados, "novos": novos, "quedas": quedas,
            "com_preco": com_preco, "alertas": alertas}


def _avaliar_alerta(con, usuario_id, linha, whatsapp, agora):
    """Compara o R$/m² atual com o alvo do imóvel e registra a notificação.

    Dispara uma única vez ao cruzar o alvo (valor_m2 <= alerta_m2) e volta a
    armar quando o R$/m² sobe novamente acima do alvo — evita spam.
    """
    alvo = con.execute(
        """SELECT alerta_m2, alerta_disparado, titulo
             FROM imoveis WHERE usuario_id = ? AND url = ?""",
        (usuario_id, linha["url"]),
    ).fetchone()
    if not alvo or alvo["alerta_m2"] is None or linha["valor_m2"] is None:
        return None

    if linha["valor_m2"] <= alvo["alerta_m2"]:
        if alvo["alerta_disparado"]:
            return None
        titulo = linha["titulo"] or alvo["titulo"]
        con.execute(
            "UPDATE imoveis SET alerta_disparado = 1 WHERE usuario_id = ? AND url = ?",
            (usuario_id, linha["url"]),
        )
        cur = con.execute(
            """INSERT INTO notificacoes
                 (usuario_id, url, titulo, quando, preco_num, valor_m2, alerta_m2, whatsapp, enviado)
               VALUES (?,?,?,?,?,?,?,?,0)""",
            (usuario_id, linha["url"], titulo, agora, linha["preco_num"],
             linha["valor_m2"], alvo["alerta_m2"], whatsapp),
        )
        logger.info(f"[usuário {usuario_id}] Alerta disparado: {titulo} "
                    f"({linha['valor_m2']}/m² <= {alvo['alerta_m2']}/m²)")
        return {
            "id": cur.lastrowid,
            "url": linha["url"],
            "titulo": titulo,
            "valor_m2": linha["valor_m2"],
            "alerta_m2": alvo["alerta_m2"],
            "preco_num": linha["preco_num"],
            "whatsapp": whatsapp,
        }

    # voltou a ficar acima do alvo -> rearma para uma próxima queda
    if alvo["alerta_disparado"]:
        con.execute(
            "UPDATE imoveis SET alerta_disparado = 0 WHERE usuario_id = ? AND url = ?",
            (usuario_id, linha["url"]),
        )
    return None


# --------------------------------------------------------------------------- #
# Leitura
# --------------------------------------------------------------------------- #

def listar_imoveis(usuario_id, valor_m2_max=None, bairro=None, quartos_min=None,
                   so_queda=False, so_com_preco=True, ordenar="valor_m2",
                   ordem="asc", limite=None):
    sql = "SELECT * FROM imoveis WHERE usuario_id = ? AND ativo = 1"
    args = [usuario_id]

    if so_com_preco:
        sql += " AND valor_m2 IS NOT NULL"
    if valor_m2_max is not None:
        sql += " AND valor_m2 <= ?"
        args.append(float(valor_m2_max))
    if bairro:
        sql += " AND (localizacao LIKE ? OR rua LIKE ? OR titulo LIKE ?)"
        args += [f"%{bairro}%"] * 3
    if quartos_min:
        sql += " AND quartos_num >= ?"
        args.append(int(quartos_min))
    if so_queda:
        sql += " AND variacao < 0"

    coluna = ORDENAVEIS.get(str(ordenar), "valor_m2")
    direcao = "DESC" if str(ordem).lower() == "desc" else "ASC"
    sql += f" ORDER BY {coluna} IS NULL, {coluna} {direcao}"
    if limite:
        sql += " LIMIT ?"
        args.append(int(limite))

    con = conectar()
    try:
        linhas = con.execute(sql, args).fetchall()
    finally:
        con.close()

    return [_item(l) for l in linhas]


def _item(l):
    primeira = l["primeira_vez"]
    ultima = l["ultima_vez"] or ""
    return {
        "url": l["url"],
        "titulo": l["titulo"],
        "localizacao": l["localizacao"],
        "rua": l["rua"],
        "condominio": l["condominio"],
        "area": l["area"],
        "quartos": l["quartos"],
        "banheiros": l["banheiros"],
        "vagas": l["vagas"],
        "imagens": l["imagens"],
        "preco": l["preco"],
        "preco_num": l["preco_num"],
        "valor_m2": l["valor_m2"],
        "novo": bool(primeira) and primeira[:10] == ultima[:10],
        "variacao": l["variacao"],
        "primeira_vez": primeira,
        "ultima_vez": ultima,
        "alerta_m2": l["alerta_m2"],
        "alerta_disparado": bool(l["alerta_disparado"]),
    }


def estatisticas(usuario_id):
    con = conectar()
    try:
        r = con.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN valor_m2 IS NOT NULL THEN 1 ELSE 0 END) AS com_preco,
                      SUM(CASE WHEN variacao < 0 THEN 1 ELSE 0 END) AS quedas,
                      SUM(CASE WHEN substr(COALESCE(primeira_vez,''),1,10) = substr(COALESCE(ultima_vez,''),1,10)
                               THEN 1 ELSE 0 END) AS novos,
                      SUM(CASE WHEN alerta_m2 IS NOT NULL THEN 1 ELSE 0 END) AS alertas,
                      SUM(CASE WHEN alerta_disparado = 1 THEN 1 ELSE 0 END) AS alertas_disparados
               FROM imoveis WHERE usuario_id = ? AND ativo = 1""",
            (usuario_id,),
        ).fetchone()
        n = con.execute(
            "SELECT COUNT(*) AS n FROM notificacoes WHERE usuario_id = ?", (usuario_id,)
        ).fetchone()["n"]
    finally:
        con.close()
    return {
        "total": r["total"] or 0,
        "com_preco": r["com_preco"] or 0,
        "quedas": r["quedas"] or 0,
        "novos": r["novos"] or 0,
        "alertas": r["alertas"] or 0,
        "alertas_disparados": r["alertas_disparados"] or 0,
        "notificacoes": n or 0,
    }


# --------------------------------------------------------------------------- #
# Alertas de queda de preço (R$/m²) e notificações
# --------------------------------------------------------------------------- #

def definir_alerta(usuario_id, url, valor_m2):
    """Define/atualiza o teto de R$/m² do imóvel. Vazio/None remove o alerta.

    Sempre rearma o disparo: um novo alvo começa "armado" (alerta_disparado = 0).
    """
    con = conectar()
    try:
        if valor_m2 in (None, ""):
            cur = con.execute(
                """UPDATE imoveis SET alerta_m2 = NULL, alerta_disparado = 0
                    WHERE usuario_id = ? AND url = ?""",
                (usuario_id, url),
            )
        else:
            cur = con.execute(
                """UPDATE imoveis SET alerta_m2 = ?, alerta_disparado = 0
                    WHERE usuario_id = ? AND url = ?""",
                (float(valor_m2), usuario_id, url),
            )
        con.commit()
        return cur.rowcount
    finally:
        con.close()


def listar_notificacoes(usuario_id, limite=100):
    """Notificações de alerta do usuário, das mais recentes para as mais antigas."""
    con = conectar()
    try:
        linhas = con.execute(
            """SELECT id, url, titulo, quando, preco_num, valor_m2, alerta_m2, whatsapp, enviado
                 FROM notificacoes WHERE usuario_id = ?
                ORDER BY quando DESC, id DESC LIMIT ?""",
            (usuario_id, int(limite)),
        ).fetchall()
    finally:
        con.close()
    return [dict(l) for l in linhas]


def limpar_notificacoes(usuario_id):
    con = conectar()
    try:
        con.execute("DELETE FROM notificacoes WHERE usuario_id = ?", (usuario_id,))
        con.commit()
    finally:
        con.close()


def marcar_enviada(usuario_id, notificacao_id, enviada=True):
    """Marca (ou desmarca) uma notificação como entregue pelo canal externo."""
    con = conectar()
    try:
        con.execute(
            "UPDATE notificacoes SET enviado = ? WHERE usuario_id = ? AND id = ?",
            (1 if enviada else 0, usuario_id, int(notificacao_id)),
        )
        con.commit()
    finally:
        con.close()


def historico_precos(usuario_id, url):
    con = conectar()
    try:
        linhas = con.execute(
            "SELECT quando, preco_num FROM precos_hist WHERE usuario_id = ? AND url = ? ORDER BY quando",
            (usuario_id, url),
        ).fetchall()
    finally:
        con.close()
    return [{"quando": l["quando"], "preco_num": l["preco_num"]} for l in linhas]


# --------------------------------------------------------------------------- #
# Suporte (pedidos do usuário: bug, melhoria ou novo site)
# --------------------------------------------------------------------------- #

def registrar_suporte(usuario_id, tipo, assunto, mensagem, enviado=False):
    """Grava um pedido de suporte. Fica no painel mesmo se o e-mail falhar."""
    con = conectar()
    try:
        cur = con.execute(
            """INSERT INTO suporte (usuario_id, tipo, assunto, mensagem, quando, enviado)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (usuario_id, tipo, assunto, mensagem,
             datetime.now().isoformat(timespec="seconds"), 1 if enviado else 0),
        )
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# Exportação
# --------------------------------------------------------------------------- #

def itens_para_csv(itens, campos=None):
    campos = campos or CAMPOS_CSV
    buf = io.StringIO()
    escritor = csv.DictWriter(buf, fieldnames=campos, extrasaction="ignore", lineterminator="\n")
    escritor.writeheader()
    for it in itens:
        escritor.writerow({c: ("" if it.get(c) is None else it.get(c)) for c in campos})
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# Seed: importa os CSVs legados para o usuário informado
# --------------------------------------------------------------------------- #

def seed_de_csvs(usuario_id):
    """Se o usuário ainda não tem imóveis, importa os CSVs existentes do projeto."""
    con = conectar()
    try:
        ja_tem = con.execute(
            "SELECT COUNT(*) AS n FROM imoveis WHERE usuario_id = ?", (usuario_id,)
        ).fetchone()["n"]
    finally:
        con.close()
    if ja_tem:
        return 0

    importados = 0
    for nome in ("imoveis_filtrados.csv", "imoveis_zap.csv"):
        caminho = os.path.join(BASE_DIR, nome)
        if not os.path.isfile(caminho):
            continue
        with open(caminho, encoding="utf-8-sig", newline="") as f:
            registros = [r for r in csv.DictReader(f) if _texto(r.get("url"))]
        if not registros:
            continue
        resumo = salvar_registros(registros, usuario_id,
                                  url_busca=f"seed:{nome}", registrar_captura=False)
        importados += resumo["total"]

    if importados:
        logger.info(f"Seed concluído: {importados} imóveis importados dos CSVs antigos.")
    return importados


# --------------------------------------------------------------------------- #
# Manutenção
# --------------------------------------------------------------------------- #

def limpar_tudo(usuario_id):
    """Apaga os imóveis, o histórico e as capturas do usuário (workspace)."""
    con = conectar()
    try:
        con.execute("DELETE FROM imoveis WHERE usuario_id = ?", (usuario_id,))
        con.execute("DELETE FROM precos_hist WHERE usuario_id = ?", (usuario_id,))
        con.execute("DELETE FROM capturas WHERE usuario_id = ?", (usuario_id,))
        con.execute("DELETE FROM notificacoes WHERE usuario_id = ?", (usuario_id,))
        con.commit()
    finally:
        con.close()
    logger.info(f"[usuário {usuario_id}] Workspace limpo.")


def excluir_imovel(usuario_id, url):
    """Tira um imóvel do monitoramento do usuário (exclusão lógica).

    O registro permanece no banco (histórico de preços preservado), mas com
    `ativo = 0` deixa de aparecer na listagem e nas estatísticas do usuário.
    A exclusão é mantida nas próximas capturas, pois a atualização não reativa
    o imóvel.
    """
    con = conectar()
    try:
        cur = con.execute(
            "UPDATE imoveis SET ativo = 0 WHERE usuario_id = ? AND url = ?",
            (usuario_id, url),
        )
        con.commit()
        removidos = cur.rowcount
    finally:
        con.close()
    if removidos:
        logger.info(f"[usuário {usuario_id}] Imóvel removido do monitoramento: {url}")
    return removidos
