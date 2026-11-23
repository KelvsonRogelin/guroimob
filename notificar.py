# -*- coding: utf-8 -*-
"""Envio das notificações de alerta do Guru Imob.

Canal principal: WhatsApp Cloud API (Meta). Quando as credenciais não estão
no .env, o sistema mantém o comportamento anterior (fila local + link wa.me)
sem quebrar nada.
"""
import logging
import os
import smtplib
from email.message import EmailMessage
from email.utils import formataddr

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

GRAPH_VERSION = os.getenv("WHATSAPP_GRAPH_VERSION") or "v21.0"
TIMEOUT = 20


def configurado():
    """True quando token e phone number ID estão presentes no .env."""
    return bool(os.getenv("WHATSAPP_TOKEN")) and bool(os.getenv("WHATSAPP_PHONE_ID"))


def fmt_brl(v):
    """Formata um número no padrão brasileiro (R$ 1.234,56)."""
    if v is None:
        return "-"
    return "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def montar_mensagem(titulo, valor_m2, alerta_m2, preco_num, url):
    """Texto enviado no alerta (mesmo conteúdo exibido no painel)."""
    return (
        f"Guru Imob - alerta de queda de preco\n"
        f"{titulo or 'Imovel monitorado'}\n"
        f"R$/m2 atual: {fmt_brl(valor_m2)} (alvo: {fmt_brl(alerta_m2)})\n"
        f"Preco do anuncio: {fmt_brl(preco_num)}\n"
        f"{url}"
    )


def enviar(numero, texto):
    """Envia um texto pela Cloud API da Meta. Retorna (ok, detalhe)."""
    if not configurado():
        return False, "not_configured"
    if not numero:
        return False, "sem_numero"

    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{os.getenv('WHATSAPP_PHONE_ID')}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": numero,
        "type": "text",
        "text": {"preview_url": False, "body": texto},
    }
    headers = {
        "Authorization": f"Bearer {os.getenv('WHATSAPP_TOKEN')}",
        "Content-Type": "application/json",
    }

    try:
        r = requests.post(url, json=payload, headers=headers, timeout=TIMEOUT)
    except requests.RequestException as e:
        logger.error(f"Falha de rede ao enviar WhatsApp: {e}")
        return False, f"erro_rede: {e}"

    if r.status_code >= 400:
        detalhe = r.text[:500]
        logger.error(f"WhatsApp recusou o envio ({r.status_code}): {detalhe}")
        return False, f"http_{r.status_code}: {detalhe}"

    wid = ((r.json().get("messages") or [{}])[0]).get("id")
    logger.info(f"WhatsApp enviado para {numero} (id={wid})")
    return True, wid


def despachar(alertas):
    """Envia cada alerta gerado numa captura.

    Recebe os dicionários devolvidos por db.salvar_registros (cada um traz o
    id da notificação gravada) e devolve uma lista com o resultado de cada
    tentativa: {"id", "ok", "detalhe"}.
    """
    if not configurado():
        return []

    resultados = []
    for a in alertas or []:
        numero = a.get("whatsapp")
        if not numero:
            continue
        texto = montar_mensagem(a.get("titulo"), a.get("valor_m2"),
                                a.get("alerta_m2"), a.get("preco_num"), a.get("url"))
        ok, detalhe = enviar(numero, texto)
        resultados.append({"id": a.get("id"), "ok": ok, "detalhe": detalhe})
    return resultados


# --------------------------------------------------------------------------- #
# E-mail (pedidos de suporte) — SMTP, ex.: Gmail com "senha de app"
# --------------------------------------------------------------------------- #

def email_configurado():
    """True quando o SMTP e o e-mail de destino estão presentes no .env."""
    return bool(os.getenv("SMTP_HOST") and os.getenv("SMTP_USER")
                and os.getenv("SMTP_PASS") and os.getenv("SUPPORT_EMAIL"))


def enviar_email(assunto, corpo, reply_to=None):
    """Envia um e-mail pelo SMTP configurado no .env.

    Retorna (ok, detalhe): (True, destinatário) quando enviado, ou
    (False, motivo) quando não configurado ou houve falha.
    """
    if not email_configurado():
        return False, "not_configured"

    host = os.getenv("SMTP_HOST")
    porta = int(os.getenv("SMTP_PORT") or 587)
    usuario = os.getenv("SMTP_USER")
    senha = os.getenv("SMTP_PASS")
    remetente = os.getenv("SMTP_FROM") or usuario
    destino = os.getenv("SUPPORT_EMAIL")

    msg = EmailMessage()
    msg["Subject"] = assunto
    msg["From"] = formataddr(("Guru Imob - Suporte", remetente))
    msg["To"] = destino
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(corpo)

    try:
        with smtplib.SMTP(host, porta, timeout=TIMEOUT) as s:
            s.ehlo()
            s.starttls()
            s.login(usuario, senha)
            s.send_message(msg)
    except Exception as e:
        logger.error(f"Falha ao enviar e-mail de suporte: {e}")
        return False, f"erro_email: {e}"

    logger.info(f"E-mail de suporte enviado para {destino}.")
    return True, destino
