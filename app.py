import json
import logging
import re
from flask import Flask, request, jsonify, send_file, send_from_directory, session
from flask import redirect
from werkzeug.middleware.proxy_fix import ProxyFix
from datetime import datetime, timedelta
from urllib.parse import quote
import os
from dotenv import load_dotenv
from zap_imoveis import (
    capturar_listings_renderizados,
    detectar_portal,
    listar_portais,
    resolver_rolagens,
)
from sheets import exportar_para_sheets
from filter import parse_brl_price, parse_area
import db
import notificar

# Configuração de Logs
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

load_dotenv()

app = Flask(__name__, static_folder=None)
app.wsgi_app = ProxyFix(app.wsgi_app)
app.secret_key = os.getenv("FLASK_SECRET", "dev-secret-change")
app.permanent_session_lifetime = timedelta(hours=12)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Inicializa o banco (fonte única de verdade), garante o schema multiusuário e
# importa CSVs legados na 1ª vez (atribuídos à conta administradora).
db.init_db()
ADMIN_ID = db.migrar_multiusuario(
    os.getenv("ADMIN_EMAIL", "admin@imobscout.local"),
    os.getenv("ADMIN_PASS", os.getenv("AUTH_PASS", "admin")),
    os.getenv("ADMIN_NOME", "Administrador"),
)
db.seed_de_csvs(ADMIN_ID)

# CSVs que podem ser baixados pela interface (nomes lógicos; no disco ficam
# escopados por usuário para garantir o isolamento entre workspaces).
CSV_DOWNLOADS = ("imoveis_zap.csv", "imoveis_filtrados.csv")


def _csv_path(nome, uid=None):
    """Caminho de um arquivo de trabalho, opcionalmente escopado por usuário."""
    prefixo = f"u{uid}_" if uid is not None else ""
    return os.path.join(BASE_DIR, f"{prefixo}{nome}")


def _regenerar_csvs(uid, valor_m2_max=None):
    """Regenera os CSVs do usuário a partir do banco (download na interface)."""
    todos = db.listar_imoveis(uid, so_com_preco=False, ordenar="quando", ordem="desc")
    with open(_csv_path("imoveis_zap.csv", uid), "w", encoding="utf-8", newline="") as f:
        f.write(db.itens_para_csv(todos))
    filtrados = db.listar_imoveis(uid, valor_m2_max=valor_m2_max, so_com_preco=True,
                                  ordenar="valor_m2", ordem="asc")
    with open(_csv_path("imoveis_filtrados.csv", uid), "w", encoding="utf-8", newline="") as f:
        f.write(db.itens_para_csv(filtrados))
    return len(todos), len(filtrados)


def is_logged():
    return bool(session.get("uid"))


def require_login():
    if not is_logged():
        return jsonify({"ok": False, "error": "auth_required"}), 401


def uid():
    """Id do usuário logado (workspace ativo)."""
    return session.get("uid")


def _normalizar_whatsapp(numero):
    """Mantém apenas dígitos e garante o DDI 55 para números do Brasil."""
    digitos = re.sub(r"\D", "", numero or "")
    if not digitos:
        return None
    if len(digitos) in (10, 11):
        digitos = "55" + digitos
    return digitos


def _link_whatsapp(numero, texto):
    """Monta o link wa.me (fila local: abre o WhatsApp já com a mensagem)."""
    if not numero:
        return None
    return f"https://wa.me/{numero}?text={quote(texto)}"


def _fmt_brl(v):
    return notificar.fmt_brl(v)


def _mensagem_alerta(titulo, valor_m2, alerta_m2, preco_num, url):
    return notificar.montar_mensagem(titulo, valor_m2, alerta_m2, preco_num, url)


def _enviar_alertas(usuario_id, alertas):
    """Envia os alertas da captura pelo WhatsApp e marca os que saíram.

    Sem credenciais no .env, o envio é simplesmente ignorado: os alertas
    seguem disponíveis no painel com o link wa.me.
    """
    if not alertas or not notificar.configurado():
        return 0
    enviados = 0
    for r in notificar.despachar(alertas):
        if r["ok"]:
            enviados += 1
            if r.get("id"):
                db.marcar_enviada(usuario_id, r["id"], True)
    return enviados


def _media_m2(registros):
    """Calcula o R$/m² da região a partir dos imóveis coletados numa varredura.

    Usa média (pedido do usuário) e mediana (mais robusta a outliers) sobre os
    anúncios que trazem preço e área ao mesmo tempo.
    """
    valores = []
    com_preco = 0
    for reg in registros:
        preco_txt = reg.get("preco")
        area_txt = reg.get("area")
        preco = parse_brl_price(str(preco_txt)) if preco_txt else None
        area = parse_area(str(area_txt)) if area_txt else None
        if preco:
            com_preco += 1
        if preco and area and area > 0:
            valores.append(preco / area)

    if not valores:
        return {
            "ok": True, "amostra": 0, "com_preco": com_preco,
            "media": None, "mediana": None, "minimo": None, "maximo": None,
        }

    valores.sort()
    n = len(valores)
    media = sum(valores) / n
    mediana = valores[n // 2] if n % 2 else (valores[n // 2 - 1] + valores[n // 2]) / 2
    return {
        "ok": True,
        "amostra": n,
        "com_preco": com_preco,
        "media": round(media, 2),
        "mediana": round(mediana, 2),
        "minimo": round(valores[0], 2),
        "maximo": round(valores[-1], 2),
    }


@app.get("/")
def root():
    if is_logged():
        return redirect("/home")
    return send_from_directory(os.path.dirname(__file__), "login.html")


@app.get("/cadastro")
def cadastro():
    if is_logged():
        return redirect("/home")
    return send_from_directory(os.path.dirname(__file__), "cadastro.html")


@app.get("/home")
def home():
    if not is_logged():
        return redirect("/")
    return send_from_directory(os.path.dirname(__file__), "index.html")


@app.get("/portais")
def portais():
    """Página informativa com os portais que a aplicação consegue mapear."""
    if not is_logged():
        return redirect("/")
    return send_from_directory(os.path.dirname(__file__), "portais.html")


@app.get("/logo/<path:nome>")
def serve_logo(nome):
    return send_from_directory(os.path.join(os.path.dirname(__file__), "logo"), nome)


@app.get("/api/session")
def api_session():
    if not is_logged():
        return jsonify({"ok": True, "logged": False})
    usuario = session.get("user") or {}
    u = db.obter_usuario(uid())
    if u:
        usuario = {**usuario, "whatsapp": u.get("whatsapp")}
        session["user"] = usuario
    return jsonify({"ok": True, "logged": True, "user": usuario})


@app.post("/api/whatsapp")
def api_whatsapp():
    """Cadastra/atualiza o número de WhatsApp do usuário (destino das notificações)."""
    r = require_login()
    if r: return r
    data = request.get_json(silent=True) or {}
    numero = _normalizar_whatsapp(data.get("numero"))
    if data.get("numero") and not numero:
        return jsonify({"ok": False, "error": "invalid_number",
                        "msg": "Informe um número válido com DDD."}), 400
    db.definir_whatsapp(uid(), numero)
    usuario = dict(session.get("user") or {})
    usuario["whatsapp"] = numero
    session["user"] = usuario
    return jsonify({"ok": True, "whatsapp": numero})


@app.post("/api/whatsapp/teste")
def api_whatsapp_teste():
    """Envia uma mensagem manual de teste para o WhatsApp cadastrado do usuário."""
    r = require_login()
    if r: return r
    usuario_id = uid()
    usuario = db.obter_usuario(usuario_id) or {}
    numero = usuario.get("whatsapp")
    if not numero:
        return jsonify({"ok": False, "error": "missing_whatsapp",
                        "msg": "Cadastre um WhatsApp antes de enviar o teste."}), 400
    if not notificar.configurado():
        return jsonify({"ok": False, "error": "not_configured",
                        "msg": "As credenciais da Meta ainda não foram carregadas no servidor."}), 400

    texto = (
        "Guru Imob - teste de notificacao\n"
        "Se voce recebeu esta mensagem, o envio manual pelo painel esta funcionando.\n"
        f"Destino: {numero}"
    )
    ok, detalhe = notificar.enviar(numero, texto)
    if not ok:
        return jsonify({"ok": False, "error": "send_failed",
                        "msg": "A Meta recusou o envio do teste.",
                        "detalhe": detalhe}), 502

    logger.info(f"[usuário {usuario_id}] Teste manual de WhatsApp enviado para {numero} (id={detalhe})")
    return jsonify({
        "ok": True,
        "whatsapp": numero,
        "wamid": detalhe,
        "msg": "Mensagem de teste enviada com sucesso.",
    })


@app.post("/api/perfil")
def api_perfil():
    """Atualiza nome e e-mail do usuário logado."""
    r = require_login()
    if r: return r
    data = request.get_json(silent=True) or {}
    nome = (data.get("nome") or "").strip()
    email = (data.get("email") or "").strip()

    if not nome:
        return jsonify({"ok": False, "error": "missing_nome", "msg": "Informe o seu nome."}), 400
    if "@" not in email or "." not in email.split("@")[-1]:
        return jsonify({"ok": False, "error": "invalid_email", "msg": "Informe um e-mail válido."}), 400

    erro = db.atualizar_perfil(uid(), nome, email)
    if erro == "email_exists":
        return jsonify({"ok": False, "error": "email_exists",
                        "msg": "Este e-mail já está em uso por outra conta."}), 409
    if erro:
        return jsonify({"ok": False, "error": erro, "msg": "Usuário não encontrado."}), 404

    usuario = dict(session.get("user") or {})
    usuario["nome"], usuario["email"] = nome, email
    session["user"] = usuario
    return jsonify({"ok": True, "user": usuario})


@app.post("/api/senha")
def api_senha():
    """Troca a senha do usuário logado após validar a senha atual."""
    r = require_login()
    if r: return r
    data = request.get_json(silent=True) or {}
    atual = data.get("atual") or ""
    nova = data.get("nova") or ""
    confirma = data.get("confirma") or ""

    if len(nova) < 6:
        return jsonify({"ok": False, "error": "weak_password",
                        "msg": "A nova senha deve ter ao menos 6 caracteres."}), 400
    if nova != confirma:
        return jsonify({"ok": False, "error": "password_mismatch",
                        "msg": "As senhas não conferem."}), 400
    if not db.alterar_senha(uid(), atual, nova):
        return jsonify({"ok": False, "error": "invalid_password",
                        "msg": "A senha atual está incorreta."}), 400
    return jsonify({"ok": True, "msg": "Senha alterada com sucesso."})


# Tipos de pedido aceitos no suporte (chave -> rótulo legível do e-mail).
TIPOS_SUPORTE = {
    "bug": "Reportar um problema (bug)",
    "melhoria": "Pedir uma melhoria",
    "site": "Sugerir um novo site",
}


@app.post("/api/suporte")
def api_suporte():
    """Recebe um pedido de suporte e envia por e-mail para a equipe.

    O pedido é sempre registrado no banco (nada se perde). Se o SMTP ainda não
    estiver configurado no .env, o e-mail não sai e a resposta indica isso.
    """
    r = require_login()
    if r: return r
    data = request.get_json(silent=True) or {}
    tipo = (data.get("tipo") or "").strip()
    assunto = (data.get("assunto") or "").strip()
    mensagem = (data.get("mensagem") or "").strip()

    if tipo not in TIPOS_SUPORTE:
        return jsonify({"ok": False, "error": "invalid_tipo",
                        "msg": "Escolha o tipo do seu pedido."}), 400
    if not assunto:
        return jsonify({"ok": False, "error": "missing_assunto",
                        "msg": "Informe o assunto do pedido."}), 400
    if not mensagem:
        return jsonify({"ok": False, "error": "missing_mensagem",
                        "msg": "Descreva o seu pedido nos detalhes."}), 400
    if len(mensagem) > 5000:
        return jsonify({"ok": False, "error": "too_long",
                        "msg": "O texto ficou muito longo. Resuma um pouco."}), 400

    usuario_id = uid()
    u = db.obter_usuario(usuario_id) or {}
    rotulo = TIPOS_SUPORTE[tipo]
    remetente_email = u.get("email")

    corpo = (
        f"Tipo: {rotulo}\n"
        f"Assunto: {assunto}\n"
        f"Usuário: {u.get('nome') or '-'} (id {usuario_id})\n"
        f"E-mail: {remetente_email or '-'}\n"
        f"Quando: {datetime.now().isoformat(timespec='seconds')}\n"
        f"\n{mensagem}\n"
    )
    assunto_email = f"[Guru Imob][{rotulo}] {assunto}"

    enviado, detalhe = notificar.enviar_email(assunto_email, corpo,
                                              reply_to=remetente_email)
    db.registrar_suporte(usuario_id, tipo, assunto, mensagem, enviado=enviado)

    if enviado:
        logger.info(f"[usuário {usuario_id}] Suporte '{tipo}' enviado para {detalhe}.")
        return jsonify({"ok": True, "enviado": True,
                        "msg": "Recebemos o seu pedido. Obrigado pelo contato!"})

    logger.warning(f"[usuário {usuario_id}] Suporte '{tipo}' registrado, "
                   f"mas o e-mail não saiu ({detalhe}).")
    return jsonify({
        "ok": True, "enviado": False,
        "msg": "Pedido registrado. O envio por e-mail ainda não está configurado, "
               "mas nada foi perdido.",
    })


@app.post("/api/alerta")
def api_alerta():
    """Define o teto de R$/m² de alerta de um imóvel. Vazio remove o alerta."""
    r = require_login()
    if r: return r
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    valor = data.get("valor_m2")
    if not url:
        return jsonify({"ok": False, "error": "missing_url"}), 400

    valor_num = None
    if valor not in (None, ""):
        try:
            valor_num = float(str(valor).replace(".", "").replace(",", ".")) \
                if "," in str(valor) else float(valor)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "invalid_value",
                            "msg": "Informe um valor de R$/m² válido."}), 400
        if valor_num <= 0:
            return jsonify({"ok": False, "error": "invalid_value",
                            "msg": "O valor deve ser maior que zero."}), 400

    removidos = db.definir_alerta(uid(), url, valor_num)
    if not removidos:
        return jsonify({"ok": False, "error": "not_found"}), 404
    return jsonify({"ok": True, "url": url, "alerta_m2": valor_num,
                    "stats": db.estatisticas(uid())})


@app.get("/api/notificacoes")
def api_notificacoes():
    """Lista as notificações de alerta com o link pronto do WhatsApp."""
    r = require_login()
    if r: return r
    usuario_id = uid()
    u = db.obter_usuario(usuario_id) or {}
    numero = u.get("whatsapp")
    itens = []
    for n in db.listar_notificacoes(usuario_id):
        texto = _mensagem_alerta(n["titulo"], n["valor_m2"], n["alerta_m2"],
                                 n["preco_num"], n["url"])
        itens.append({**n, "link": _link_whatsapp(numero, texto)})
    return jsonify({"ok": True, "whatsapp": numero, "total": len(itens), "itens": itens})


@app.post("/api/notificacoes/limpar")
def api_notificacoes_limpar():
    r = require_login()
    if r: return r
    db.limpar_notificacoes(uid())
    return jsonify({"ok": True})


@app.post("/api/login")
def api_login():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or data.get("user") or "").strip()
    senha = (data.get("pass") or data.get("senha") or "").strip()
    usuario = db.autenticar(email, senha)
    if not usuario:
        return jsonify({"ok": False, "error": "invalid_credentials"}), 401
    session.permanent = True
    session["uid"] = usuario["id"]
    session["user"] = {"nome": usuario["nome"], "email": usuario["email"]}
    return jsonify({"ok": True, "user": session["user"]})


@app.post("/api/cadastro")
def api_cadastro():
    data = request.get_json(silent=True) or {}
    nome = (data.get("nome") or "").strip()
    email = (data.get("email") or "").strip()
    senha = data.get("senha") or ""
    confirma = data.get("confirma") or ""

    if not nome:
        return jsonify({"ok": False, "error": "missing_nome", "msg": "Informe o seu nome."}), 400
    if "@" not in email or "." not in email.split("@")[-1]:
        return jsonify({"ok": False, "error": "invalid_email", "msg": "Informe um e-mail válido."}), 400
    if len(senha) < 6:
        return jsonify({"ok": False, "error": "weak_password", "msg": "A senha deve ter ao menos 6 caracteres."}), 400
    if senha != confirma:
        return jsonify({"ok": False, "error": "password_mismatch", "msg": "As senhas não conferem."}), 400

    novo_id = db.criar_usuario(nome, email, senha)
    if novo_id is None:
        return jsonify({"ok": False, "error": "email_exists", "msg": "Este e-mail já está cadastrado."}), 409

    logger.info(f"Novo usuário cadastrado: {email}")
    return jsonify({"ok": True, "msg": "Conta criada com sucesso. Faça login para continuar."})


@app.post("/api/logout")
def api_logout():
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/portais")
def api_portais():
    """Lista os portais conhecidos e quantas rolagens cada 'página' representa."""
    r = require_login()
    if r: return r
    return jsonify({"ok": True, "portais": listar_portais()})


@app.get("/api/capturar")
def api_capturar():
    r = require_login()
    if r: return r
    usuario_id = uid()
    url = request.args.get("url", type=str)
    valor_m2 = request.args.get("valor_m2", type=str)
    paginas = request.args.get("paginas", type=int)

    valor_m2_num = None
    try:
        if valor_m2:
            valor_m2_num = float(str(valor_m2).replace(",", "."))
    except:
        valor_m2_num = None
    if not url:
        return jsonify({"ok": False, "error": "missing_url"}), 400

    rolagens = resolver_rolagens(url, paginas) or 25
    _, portal = detectar_portal(url)

    saida_json = _csv_path("imoveis_zap.json", usuario_id)
    saida_csv = _csv_path("imoveis_zap.csv", usuario_id)

    total = capturar_listings_renderizados(
        url=url,
        saida_json=saida_json,
        saida_csv=saida_csv,
        headless=True,
        max_loops=rolagens,
    )

    registros = []
    if total > 0 and os.path.isfile(saida_json):
        with open(saida_json, encoding="utf-8") as f:
            registros = json.load(f)

    resumo = db.salvar_registros(registros, usuario_id, url_busca=url) if registros else \
        {"total": 0, "novos": 0, "quedas": 0, "com_preco": 0, "alertas": []}
    enviados = _enviar_alertas(usuario_id, resumo.get("alertas") or [])
    _regenerar_csvs(usuario_id, valor_m2_num)

    return jsonify({
        "ok": True,
        "capturados": total,
        "filtrados": resumo["com_preco"],
        "novos": resumo["novos"],
        "quedas": resumo["quedas"],
        "alertas": len(resumo.get("alertas") or []),
        "enviados": enviados,
        "canal": "whatsapp" if notificar.configurado() else "fila_local",
        "portal": portal["nome"],
        "paginas": paginas,
        "rolagens": rolagens,
        "stats": db.estatisticas(usuario_id),
        "csvs": {
            "zap": "/api/csv/imoveis_zap.csv",
            "filtrados": "/api/csv/imoveis_filtrados.csv"
        }
    })


@app.get("/api/imoveis")
def api_imoveis():
    r = require_login()
    if r: return r
    usuario_id = uid()

    valor_m2 = request.args.get("valor_m2", type=str)
    try:
        valor_m2_max = float(str(valor_m2).replace(",", ".")) if valor_m2 else None
    except:
        valor_m2_max = None

    itens = db.listar_imoveis(
        usuario_id,
        valor_m2_max=valor_m2_max,
        bairro=request.args.get("bairro") or None,
        quartos_min=request.args.get("quartos", type=int),
        so_queda=request.args.get("so_queda", type=str) in ("1", "true", "sim"),
        so_com_preco=request.args.get("com_preco", type=str) not in ("0", "false"),
        ordenar=request.args.get("ordenar", default="valor_m2"),
        ordem=request.args.get("ordem", default="asc"),
        limite=request.args.get("limite", type=int),
    )
    return jsonify({"ok": True, "total": len(itens),
                    "stats": db.estatisticas(usuario_id), "itens": itens})


@app.get("/api/historico")
def api_historico():
    r = require_login()
    if r: return r
    url = request.args.get("url", type=str)
    if not url:
        return jsonify({"ok": False, "error": "missing_url"}), 400
    return jsonify({"ok": True, "itens": db.historico_precos(uid(), url)})


@app.post("/api/limpar")
def api_limpar():
    """Apaga os dados e os CSVs do workspace do usuário logado."""
    r = require_login()
    if r: return r
    usuario_id = uid()

    db.limpar_tudo(usuario_id)
    for nome in ("imoveis_zap.csv", "imoveis_filtrados.csv", "imoveis_zap.json"):
        caminho = _csv_path(nome, usuario_id)
        if os.path.isfile(caminho):
            os.remove(caminho)
    _regenerar_csvs(usuario_id)
    logger.info(f"[usuário {usuario_id}] Limpeza solicitada: banco e CSVs zerados.")
    return jsonify({"ok": True, "stats": db.estatisticas(usuario_id)})


@app.post("/api/excluir")
def api_excluir():
    """Remove um imóvel do monitoramento (o histórico permanece no banco)."""
    r = require_login()
    if r: return r
    usuario_id = uid()

    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"ok": False, "error": "missing_url"}), 400

    removidos = db.excluir_imovel(usuario_id, url)
    _regenerar_csvs(usuario_id)
    return jsonify({"ok": True, "removidos": removidos,
                    "stats": db.estatisticas(usuario_id)})


@app.post("/api/varrer")
def api_varrer():
    """Varre uma URL já filtrada do ZAP e devolve o preço médio do m² da região.

    Diferente de /api/capturar, esta varredura é apenas analítica: não grava no
    banco nem sobrescreve os CSVs monitorados (usa arquivos temporários).
    """
    r = require_login()
    if r: return r
    usuario_id = uid()

    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"ok": False, "error": "missing_url"}), 400

    rolagens = resolver_rolagens(url, data.get("paginas")) or 25
    _, portal = detectar_portal(url)

    tmp_json = _csv_path("_varredura.json", usuario_id)
    tmp_csv = _csv_path("_varredura.csv", usuario_id)
    try:
        total = capturar_listings_renderizados(
            url=url,
            saida_json=tmp_json,
            saida_csv=tmp_csv,
            headless=True,
            max_loops=rolagens,
        )
        registros = []
        if total > 0 and os.path.isfile(tmp_json):
            with open(tmp_json, encoding="utf-8") as f:
                registros = json.load(f)

        resumo = _media_m2(registros)
        resumo.update({
            "capturados": total,
            "url": url,
            "portal": portal["nome"],
            "paginas": data.get("paginas"),
            "rolagens": rolagens,
        })
        return jsonify(resumo)
    except Exception as e:
        logger.error(f"Erro na varredura: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        for caminho in (tmp_json, tmp_csv):
            if os.path.isfile(caminho):
                os.remove(caminho)


@app.post("/api/exportar-sheets")
def api_exportar_sheets():
    r = require_login()
    if r: return r
    usuario_id = uid()

    data = request.get_json(silent=True) or {}
    sheet_name = data.get("sheet_name", "Imoveis Filtrados")

    try:
        _regenerar_csvs(usuario_id)
        sucesso = exportar_para_sheets(
            csv_path=_csv_path("imoveis_filtrados.csv", usuario_id),
            sheet_name=sheet_name,
        )
        if sucesso:
            return jsonify({"ok": True, "msg": f"Exportado para '{sheet_name}' com sucesso."})
        else:
            return jsonify({"ok": False, "error": "export_failed", "msg": "Falha na exportação. Verifique logs."}), 500
    except Exception as e:
        logger.error(f"Erro no endpoint de exportação: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.get("/api/csv/<path:name>")
def api_csv(name):
    r = require_login()
    if r: return r
    if name not in CSV_DOWNLOADS:
        return jsonify({"ok": False, "error": "not_found"}), 404
    fp = _csv_path(name, uid())
    if not os.path.isfile(fp):
        return jsonify({"ok": False, "error": "not_found"}), 404
    return send_file(fp, as_attachment=False, download_name=name,
                     mimetype="text/csv; charset=utf-8")


def run():
    # Padrão: só a sua máquina. Defina APP_HOST=0.0.0.0 no .env para liberar
    # o acesso pela rede privada (ex.: Tailscale) mantendo o firewall restrito.
    host = os.getenv("APP_HOST", "127.0.0.1")
    porta = int(os.getenv("APP_PORT", "5000"))
    app.run(host=host, port=porta, debug=True)


if __name__ == "__main__":
    run()
