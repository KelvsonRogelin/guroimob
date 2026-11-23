import json
import logging
import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import pandas as pd
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

# Configuração básica de log (pode ser sobrescrita pelo app principal)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}
BROWSER_VIEWPORT = {"width": 1366, "height": 900}
TIMEOUT_LOAD = 60000
TIMEOUT_SELECTOR = 15000
TIMEOUT_CLICK = 1500
REQUEST_TIMEOUT = 30

ZAP_SELECTORS = {
    "COOKIE_BANNERS": [
        'button:has-text("Aceitar")',
        'button:has-text("Aceitar todos")',
        'button:has-text("Concordo")',
        'button:has-text("Prosseguir")',
        'button:has-text("Aceitar e continuar")',
        "#onetrust-accept-btn-handler",
        '[data-testid="accept-all"]',
        '[data-testid="cookie-banner-accept"]',
        'button[aria-label="Aceitar"]',
    ],
    "CARD_CONTAINER": 'li[data-cy="rp-property-cd"]',
    "CARD_LINK": "a",
    "CARD_LOCATION": '[data-cy="rp-cardProperty-location-txt"]',
    "CARD_STREET": '[data-cy="rp-cardProperty-street-txt"]',
    "CARD_PRICE_BOX": '[data-cy="rp-cardProperty-price-txt"]',
    "CARD_AREA": '[data-cy="rp-cardProperty-propertyArea-txt"]',
    "CARD_QUARTOS": '[data-cy="rp-cardProperty-bedroomQuantity-txt"]',
    "CARD_BANHEIROS": '[data-cy="rp-cardProperty-bathroomQuantity-txt"]',
    "CARD_VAGAS": '[data-cy="rp-cardProperty-parkingSpacesQuantity-txt"]',
    "CARD_IMAGES": '[data-cy="rp-cardProperty-image-img"] img',
}
OVERLAY_SELECTORS = [
    'button:has-text("Fechar")',
    'button:has-text("Agora não")',
    'button:has-text("Agora nao")',
    'button:has-text("Entendi")',
    'button:has-text("Continuar")',
    'button:has-text("Prosseguir")',
    'button:has-text("Aceitar")',
    'button:has-text("Aceitar todos")',
    'button:has-text("Concordo")',
    'button[aria-label="Fechar"]',
    'button[aria-label="Close"]',
    '[data-testid="close-modal"]',
    '[data-testid="modal-close"]',
    '[data-testid="close-button"]',
    '.olx-button--close',
    '.modal-close',
    '.close-modal',
]

# O seletor "Páginas a varrer" da interface continua sendo o controle único de
# profundidade. Para portais com scroll infinito, cada página equivale a várias
# rolagens; para portais com paginação tradicional, 1 página equivale a 1 passo.
PORTAIS = {
    "zapimoveis.com.br": {
        "nome": "ZAP Imóveis",
        "rolagens_por_pagina": 12,
        "estrategia": "scroll",
    },
    "vivareal.com.br": {
        "nome": "Viva Real",
        "rolagens_por_pagina": 12,
        "estrategia": "scroll",
    },
    "olx.com.br": {
        "nome": "OLX",
        "rolagens_por_pagina": 1,
        "estrategia": "paginado",
        "pagina_param": "o",
    },
    "chavesnamao.com.br": {
        "nome": "Chaves na Mão",
        "rolagens_por_pagina": 1,
        "estrategia": "paginado",
        "pagina_param": "pg",
    },
}
PORTAL_PADRAO = {
    "nome": "Portal não identificado",
    "rolagens_por_pagina": 12,
    "estrategia": "scroll",
}

_RE_MOEDA = re.compile(r"R\$\s*[\d\.\,]+")
_RE_AREA = re.compile(r"\d+(?:[\.,]\d+)?\s*m²", re.I)


def detectar_portal(url):
    """Descobre o portal pelo domínio da URL. Retorna (chave, config)."""
    host = (url or "").lower()
    for chave, cfg in PORTAIS.items():
        if chave in host:
            return chave, cfg
    return None, PORTAL_PADRAO


def listar_portais():
    """Lista os portais conhecidos (usado pela interface para exibir a estratégia)."""
    return [
        {
            "chave": chave,
            "nome": cfg["nome"],
            "rolagens_por_pagina": cfg["rolagens_por_pagina"],
            "estrategia": cfg.get("estrategia"),
            "pagina_param": cfg.get("pagina_param"),
        }
        for chave, cfg in PORTAIS.items()
    ]


def resolver_rolagens(url, paginas):
    """Converte a quantidade de páginas pedida em passos internos de captura."""
    try:
        p = int(paginas)
    except (TypeError, ValueError):
        return None
    if p < 1:
        return None
    _, cfg = detectar_portal(url)
    return p * cfg["rolagens_por_pagina"]


def _try_clicks(page, selectors, timeout=TIMEOUT_CLICK):
    for sel in selectors:
        try:
            page.locator(sel).first.click(timeout=timeout)
            logger.debug(f"Clique realizado em: {sel}")
            return True
        except Exception:
            pass
    return False


def _aceitar_cookies(page):
    _try_clicks(page, ZAP_SELECTORS["COOKIE_BANNERS"])


def _fechar_overlays(page):
    """Fecha modais/popups genéricos antes da leitura dos cards."""
    _aceitar_cookies(page)
    _try_clicks(page, OVERLAY_SELECTORS, timeout=1000)
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    try:
        page.evaluate(
            """() => {
                for (const sel of ['.modal-backdrop', '.ReactModal__Overlay', '[data-testid="modal-overlay"]']) {
                    document.querySelectorAll(sel).forEach(el => el.remove());
                }
                document.body.style.overflow = 'auto';
            }"""
        )
    except Exception:
        pass


def _txt(node, default=None):
    try:
        t = node.inner_text().strip()
        return t if t else default
    except Exception:
        return default


def _txt_sel(escopo, seletor, default=None):
    try:
        loc = escopo.locator(seletor)
        if loc.count() == 0:
            return default
        return _txt(loc.first, default)
    except Exception:
        return default


def _attr(node, name, default=None):
    try:
        v = node.get_attribute(name)
        return v if v else default
    except Exception:
        return default


def _extrair_preco_e_cond(texto):
    if not texto:
        return None, None
    t = " ".join(texto.split())
    m = _RE_MOEDA.search(t)
    preco = m.group().strip() if m else None

    cond = None
    mc = re.search(r"cond\.?\s*([^•]*)", t, re.I)
    if mc:
        seg = mc.group(1).strip()
        if "isento" in seg.lower():
            cond = "isento"
        else:
            mv = _RE_MOEDA.search(seg)
            cond = mv.group().strip() if mv else None
    return preco, cond


def _ultimo_numero(texto):
    if not texto:
        return None
    nums = re.findall(r"\d+", texto)
    return nums[-1] if nums else None


def _coletar_visiveis_zap(page, coletados):
    try:
        cards = page.locator(ZAP_SELECTORS["CARD_CONTAINER"])
        total = cards.count()
    except Exception:
        return

    for i in range(total):
        try:
            reg = _parse_card_zap(cards.nth(i))
        except Exception:
            continue
        url = reg.get("url")
        if not url or not reg.get("preco"):
            continue
        coletados[url] = reg


def _rolar_e_coletar_zap(page, coletados, sleep_ms=1200, max_loops=25, quiet_loops_to_stop=4, passo=800):
    prev = 0
    quiet = 0

    for i in range(max_loops):
        _coletar_visiveis_zap(page, coletados)
        page.evaluate(f"window.scrollBy(0, {passo})")
        page.wait_for_timeout(sleep_ms)
        _fechar_overlays(page)

        atual = len(coletados)
        logger.info(f"Rolagem {i + 1}/{max_loops} - imóveis coletados: {atual}")

        if atual <= prev:
            quiet += 1
        else:
            quiet = 0
        prev = atual

        if quiet >= quiet_loops_to_stop:
            logger.info("Nenhum imóvel novo após várias rolagens. Parando.")
            break

    _coletar_visiveis_zap(page, coletados)
    return coletados


def _parse_card_zap(card):
    link = card.locator(ZAP_SELECTORS["CARD_LINK"]).first
    href = _attr(link, "href")
    href = urljoin("https://www.zapimoveis.com.br", href) if href else None
    titulo = _attr(link, "title")

    loc_txt = _txt_sel(card, ZAP_SELECTORS["CARD_LOCATION"])
    rua = _txt_sel(card, ZAP_SELECTORS["CARD_STREET"])
    preco_txt, cond_txt = _extrair_preco_e_cond(_txt_sel(card, ZAP_SELECTORS["CARD_PRICE_BOX"]))

    imgs = []
    try:
        for img in card.locator(ZAP_SELECTORS["CARD_IMAGES"]).all():
            src = _attr(img, "src")
            if src and src not in imgs:
                imgs.append(src)
    except Exception:
        pass

    area = _txt_sel(card, ZAP_SELECTORS["CARD_AREA"])
    quartos = _ultimo_numero(_txt_sel(card, ZAP_SELECTORS["CARD_QUARTOS"]))
    banheiros = _ultimo_numero(_txt_sel(card, ZAP_SELECTORS["CARD_BANHEIROS"]))
    vagas = _ultimo_numero(_txt_sel(card, ZAP_SELECTORS["CARD_VAGAS"]))

    return {
        "titulo": titulo,
        "url": href,
        "localizacao": loc_txt,
        "rua": rua,
        "preco": preco_txt,
        "condominio": cond_txt,
        "area": area,
        "quartos": quartos,
        "banheiros": banheiros,
        "vagas": vagas,
        "imagens": imgs,
    }


def _slug_to_label(txt):
    txt = (txt or "").strip().replace("-", " ")
    if not txt:
        return None
    especiais = {
        "florianopolis": "Florianópolis",
    }
    base = " ".join(txt.split())
    if base.lower() in especiais:
        return especiais[base.lower()]
    return " ".join(p.capitalize() for p in base.split())


def _localizacao_vivareal(url):
    partes = [p for p in urlparse(url).path.split("/") if p]
    if len(partes) < 5:
        return None
    estado = _slug_to_label(partes[1])
    cidade = _slug_to_label(partes[2])
    bairro = _slug_to_label(partes[4]) if len(partes) > 4 and partes[3] == "bairros" else None
    if bairro and cidade and estado:
        return f"{bairro}, {cidade} - {estado}"
    if cidade and estado:
        return f"{cidade} - {estado}"
    return None


def _extrair_vivareal_pagina(page):
    return page.eval_on_selector_all(
        "a[href*='vivareal.com.br/imovel/'], a[href^='/imovel/']",
        """els => els.map(a => {
            const li = a.closest('li');
            return {
                href: a.href || '',
                text: (li?.innerText || a.innerText || a.textContent || '').trim(),
                img: li?.querySelector('img')?.src || '',
            };
        }).filter(x => x.href.includes('/imovel/') && x.text.includes('R$'))""",
    )


def _parse_vivareal_card(raw, busca_url):
    href = raw.get("href")
    if not href or "/imovel/" not in href:
        return None

    texto = " ".join((raw.get("text") or "").split())
    if not texto:
        return None

    preco, condominio = _extrair_preco_e_cond(texto)
    area_match = re.search(r"Tamanho do imóvel\s+(\d+(?:\s*-\s*\d+)?(?:[.,]\d+)?)\s*m²", texto, re.I)
    area = f"{area_match.group(1)}m²" if area_match else None
    if not area:
        area_match = _RE_AREA.search(texto)
        area = area_match.group(0).replace(" ", "") if area_match else None

    quartos = None
    banheiros = None
    vagas = None
    m = re.search(r"Quantidade de quartos\s+(\d+)", texto, re.I)
    if m:
        quartos = m.group(1)
    else:
        m = re.search(r"(\d+)\s*quartos?", texto, re.I)
        quartos = m.group(1) if m else None

    m = re.search(r"Quantidade de banheiros\s+(\d+)", texto, re.I)
    if m:
        banheiros = m.group(1)
    else:
        m = re.search(r"(\d+)\s*banheiros?", texto, re.I)
        banheiros = m.group(1) if m else None

    m = re.search(r"Quantidade de vagas\s+(\d+)", texto, re.I)
    if m:
        vagas = m.group(1)
    else:
        m = re.search(r"(\d+)\s*vagas?", texto, re.I)
        vagas = m.group(1) if m else None

    titulo = texto
    if preco and preco in titulo:
        titulo = titulo.split(preco, 1)[0].strip(" .,-")
    titulo = re.sub(r"^\+\s*\d+\s*fotos?\s*", "", titulo, flags=re.I)
    titulo = re.sub(r"^(Super Destaque|Destaque)\s*", "", titulo, flags=re.I)
    titulo = re.sub(r"\s+(Cond\.|Tamanho do imóvel|Quantidade de quartos).*$", "", titulo, flags=re.I)
    m_tipo = re.search(r"\b(Casa|Sobrado|Apartamento|Studio|Kitnet|Cobertura)\b.*$", titulo, re.I)
    if m_tipo:
        titulo = m_tipo.group(0)
    titulo = re.sub(r"\s+em$", "", titulo, flags=re.I)
    titulo = titulo.strip(" .,-")

    imagens = []
    if raw.get("img"):
        imagens.append(raw["img"])

    return {
        "titulo": titulo or "Imóvel Viva Real",
        "url": href,
        "localizacao": _localizacao_vivareal(busca_url),
        "rua": None,
        "preco": preco,
        "condominio": condominio,
        "area": area,
        "quartos": quartos,
        "banheiros": banheiros,
        "vagas": vagas,
        "imagens": imagens,
    }


def _coletar_visiveis_vivareal(page, busca_url, coletados):
    try:
        raws = _extrair_vivareal_pagina(page)
    except Exception:
        return

    for raw in raws:
        reg = _parse_vivareal_card(raw, busca_url)
        if not reg or not reg.get("url") or not reg.get("preco"):
            continue
        atual = coletados.get(reg["url"])
        if atual is None or _score_registro(reg) > _score_registro(atual):
            coletados[reg["url"]] = reg


def _rolar_e_coletar_vivareal(page, busca_url, coletados, sleep_ms=1500, max_loops=25, quiet_loops_to_stop=4, passo=900):
    prev = 0
    quiet = 0

    for i in range(max_loops):
        _coletar_visiveis_vivareal(page, busca_url, coletados)
        page.evaluate(f"window.scrollBy(0, {passo})")
        page.wait_for_timeout(sleep_ms)
        _fechar_overlays(page)

        atual = len(coletados)
        logger.info(f"Rolagem Viva Real {i + 1}/{max_loops} - imóveis coletados: {atual}")

        if atual <= prev:
            quiet += 1
        else:
            quiet = 0
        prev = atual

        if quiet >= quiet_loops_to_stop:
            logger.info("Viva Real sem novos imóveis após várias rolagens. Parando.")
            break

    _coletar_visiveis_vivareal(page, busca_url, coletados)
    return coletados


def _capturar_vivareal(url, saida_json, saida_csv, headless, sleep_ms, max_loops):
    logger.info(f"Iniciando captura Viva Real: {url}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context(
            locale="pt-BR",
            user_agent=USER_AGENT,
            viewport=BROWSER_VIEWPORT,
        )
        page = context.new_page()

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT_LOAD)
        except Exception as e:
            logger.error(f"Erro ao carregar Viva Real: {e}")
            context.close()
            browser.close()
            return 0

        _fechar_overlays(page)
        page.wait_for_timeout(3000)
        coletados = {}
        _rolar_e_coletar_vivareal(page, url, coletados, sleep_ms=sleep_ms, max_loops=max_loops)

        imoveis = list(coletados.values())
        if imoveis:
            _salvar_dados(imoveis, saida_json, saida_csv)
        context.close()
        browser.close()
        return len(imoveis)


def _extrair_json_ld(page):
    try:
        ld_blobs = page.eval_on_selector_all(
            'script[type="application/ld+json"]',
            "els => els.map(el => el.textContent)",
        )
    except Exception:
        return []

    extra = []
    for blob in ld_blobs:
        try:
            data = json.loads(blob)
            if isinstance(data, dict) and data.get("@type") in ("RealEstateListing", "Product"):
                extra.append({
                    "titulo": data.get("name"),
                    "url": data.get("url"),
                    "preco": (data.get("offers") or {}).get("price") if isinstance(data.get("offers"), dict) else None,
                })
        except Exception:
            continue
    return extra


def _capturar_zap(url, saida_json, saida_csv, headless, sleep_ms, max_loops):
    logger.info(f"Iniciando captura ZAP: {url}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context(
            locale="pt-BR",
            user_agent=USER_AGENT,
            viewport=BROWSER_VIEWPORT,
        )
        page = context.new_page()

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT_LOAD)
        except PWTimeout:
            logger.error("Timeout ao carregar a página inicial.")
            context.close()
            browser.close()
            return 0
        except Exception as e:
            logger.error(f"Erro ao carregar página: {e}")
            context.close()
            browser.close()
            return 0

        _fechar_overlays(page)

        try:
            page.wait_for_selector(ZAP_SELECTORS["CARD_CONTAINER"], timeout=TIMEOUT_SELECTOR)
        except PWTimeout:
            logger.warning("Nenhum card visível no primeiro carregamento (timeout).")

        logger.info("Executando scroll infinito (coleta incremental)...")
        coletados = {}
        _rolar_e_coletar_zap(page, coletados, sleep_ms=sleep_ms, max_loops=max_loops)

        imoveis = list(coletados.values())
        total = len(imoveis)
        logger.info(f"Total de imóveis coletados: {total}")

        if total == 0:
            logger.info("Tentando fallback via JSON-LD...")
            extra = _extrair_json_ld(page)
            if extra:
                _salvar_dados(extra, saida_json, saida_csv)
                logger.info(f"Salvo via JSON-LD: {len(extra)} itens.")
                context.close()
                browser.close()
                return len(extra)

            logger.warning("Nenhum imóvel encontrado (nem via DOM, nem via JSON-LD).")
            context.close()
            browser.close()
            return 0

        _salvar_dados(imoveis, saida_json, saida_csv)
        context.close()
        browser.close()
        return len(imoveis)


def _montar_url_paginada(url, pagina_param, pagina_num):
    partes = urlparse(url)
    query = dict(parse_qsl(partes.query, keep_blank_values=True))
    if pagina_num <= 1:
        query.pop(pagina_param, None)
    else:
        query[pagina_param] = str(pagina_num)
    nova_query = urlencode(query)
    return urlunparse(partes._replace(query=nova_query))


def _request_html(url):
    r = requests.get(url, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.text


def _linhas_limpas(texto):
    return [t.strip() for t in (texto or "").splitlines() if t and t.strip()]


def _primeira_moeda(texto):
    m = _RE_MOEDA.search(texto or "")
    return m.group().strip() if m else None


def _todas_moedas(texto):
    return [m.group().strip() for m in _RE_MOEDA.finditer(texto or "")]


def _buscar_condominio(texto):
    if not texto:
        return None
    m = re.search(r"condom[íi]nio\s*(?:r\$\s*)?([\d\.\,]+)", texto, re.I)
    if not m:
        return None
    return f"R$ {m.group(1)}"


def _parse_chaves_card(raw, base_url):
    href = urljoin(base_url, raw.get("href") or "")
    if "/imovel/" not in href:
        return None

    linhas = _linhas_limpas(raw.get("text") or "")
    if len(linhas) < 2:
        return None

    titulo = linhas[0]
    rua = linhas[1] if len(linhas) > 1 else None
    localizacao = linhas[2] if len(linhas) > 2 else None
    area = next((t for t in linhas if _RE_AREA.search(t)), None)
    preco = _primeira_moeda("\n".join(linhas))
    condominio = _buscar_condominio("\n".join(linhas))

    numeros = []
    if area and area in linhas:
        idx_area = linhas.index(area)
        for t in linhas[idx_area + 1:]:
            if t.startswith("R$"):
                break
            if re.fullmatch(r"\d+", t):
                numeros.append(t)
    quartos = numeros[0] if len(numeros) >= 1 else None
    banheiros = numeros[1] if len(numeros) >= 2 else None
    vagas = numeros[2] if len(numeros) >= 3 else None

    imagens = []
    src = raw.get("img")
    if src:
        imagens.append(urljoin(base_url, src))

    return {
        "titulo": titulo,
        "url": href,
        "localizacao": localizacao,
        "rua": rua,
        "preco": preco,
        "condominio": condominio,
        "area": area,
        "quartos": quartos,
        "banheiros": banheiros,
        "vagas": vagas,
        "imagens": imagens,
    }


def _score_registro(reg):
    """Pontua o quão completo está um registro para desempatar duplicados."""
    if not reg:
        return -1
    score = 0
    for campo in ("titulo", "rua", "localizacao", "preco", "condominio",
                  "area", "quartos", "banheiros", "vagas"):
        if reg.get(campo):
            score += 1
    score += min(len(reg.get("imagens") or []), 3)
    return score


def _extrair_chaves_pagina(page):
    return page.eval_on_selector_all(
        "a[href*='/imovel/']",
        """els => els.map(a => ({
            href: a.href || '',
            text: (a.innerText || a.textContent || '').trim(),
            img: a.querySelector('img')?.src || '',
        })).filter(x => x.text.includes('R$'))""",
    )


def _capturar_chaves(url, saida_json, saida_csv, max_passos, headless):
    paginas = max(1, int(max_passos or 1))
    coletados = {}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        page = browser.new_page(viewport=BROWSER_VIEWPORT)

        for pagina in range(1, paginas + 1):
            page_url = _montar_url_paginada(url, "pg", pagina)
            logger.info(f"Chaves na Mão - página {pagina}/{paginas}: {page_url}")
            try:
                page.goto(page_url, wait_until="domcontentloaded", timeout=TIMEOUT_LOAD)
            except Exception as e:
                logger.warning(f"Falha ao carregar página {pagina} da Chaves na Mão: {e}")
                break

            _fechar_overlays(page)
            page.wait_for_timeout(3000)
            raws = _extrair_chaves_pagina(page)
            itens_pagina = 0
            for raw in raws:
                reg = _parse_chaves_card(raw, url)
                if not reg or not reg.get("url") or not reg.get("preco"):
                    continue
                atual = coletados.get(reg["url"])
                if atual is None or _score_registro(reg) > _score_registro(atual):
                    if atual is None:
                        itens_pagina += 1
                    coletados[reg["url"]] = reg

            logger.info(f"Chaves na Mão - página {pagina}: {itens_pagina} imóveis encontrados")
            if itens_pagina == 0:
                break

        browser.close()

    imoveis = list(coletados.values())
    if imoveis:
        _salvar_dados(imoveis, saida_json, saida_csv)
    return len(imoveis)


def _parse_olx_card(raw):
    href = raw.get("href")
    if not href or "/imoveis/" not in href:
        return None

    titulo = (raw.get("text") or "").strip()
    grand_text = raw.get("grandText") or ""
    if not titulo or not grand_text:
        return None

    area = None
    quartos = None
    banheiros = None
    vagas = None
    for det in raw.get("details") or []:
        label = (det.get("aria") or "").lower()
        valor = (det.get("text") or "").strip()
        if "metro" in label and not area:
            area = valor or re.sub(r".*?(\d+(?:[\.,]\d+)?\s*m²).*", r"\1", label, flags=re.I)
        elif "quarto" in label and not quartos:
            quartos = _ultimo_numero(valor or label)
        elif "banheiro" in label and not banheiros:
            banheiros = _ultimo_numero(valor or label)
        elif "vaga" in label and not vagas:
            vagas = _ultimo_numero(valor or label)

    preco = _primeira_moeda(grand_text)
    moedas = _todas_moedas(grand_text)
    condominio = next((m for m in moedas[1:] if "condom" in grand_text[grand_text.find(m) - 20:grand_text.find(m) + len(m)].lower()), None)

    linhas = _linhas_limpas(grand_text)
    localizacao = next((t for t in linhas if "," in t and "R$" not in t and "Hoje" not in t and "Online" not in t), None)
    rua = None

    imagens = []
    img = raw.get("img")
    if img:
        imagens.append(img)

    return {
        "titulo": titulo,
        "url": href,
        "localizacao": localizacao,
        "rua": rua,
        "preco": preco,
        "condominio": condominio,
        "area": area,
        "quartos": quartos,
        "banheiros": banheiros,
        "vagas": vagas,
        "imagens": imagens,
    }


def _extrair_olx_pagina(page):
    return page.eval_on_selector_all(
        "a[href^='https://sc.olx.com.br'][href*='/imoveis/']",
        """els => els.map(a => {
            const top = a.closest('.olx-adcard__topbody') || a.parentElement?.parentElement;
            const root = top?.parentElement || top;
            const details = [...(top?.querySelectorAll('.olx-adcard__detail') || [])].map(d => ({
                aria: d.getAttribute('aria-label') || '',
                text: (d.textContent || '').trim(),
            }));
            const img = root?.querySelector('img')?.src || '';
            return {
                href: a.href || '',
                text: (a.getAttribute('title') || a.innerText || a.textContent || '').trim(),
                grandText: (root?.innerText || root?.textContent || top?.innerText || top?.textContent || '').trim(),
                details,
                img,
            };
        })""",
    )


def _capturar_olx_browser(url, saida_json, saida_csv, paginas, headless):
    coletados = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        page = browser.new_page(viewport=BROWSER_VIEWPORT)

        for pagina in range(1, paginas + 1):
            page_url = _montar_url_paginada(url, "o", pagina)
            logger.info(f"OLX - página {pagina}/{paginas}: {page_url}")
            try:
                page.goto(page_url, wait_until="domcontentloaded", timeout=TIMEOUT_LOAD)
            except Exception as e:
                logger.warning(f"Falha ao carregar página {pagina} da OLX: {e}")
                break

            _fechar_overlays(page)
            try:
                page.wait_for_selector("a[data-testid='adcard-link']", timeout=15000)
            except Exception:
                pass
            page.wait_for_timeout(5000)
            _fechar_overlays(page)
            blocos = _extrair_olx_pagina(page)
            itens_pagina = 0
            for raw in blocos:
                reg = _parse_olx_card(raw)
                if not reg or not reg.get("url") or not reg.get("preco"):
                    continue
                coletados[reg["url"]] = reg
                itens_pagina += 1

            logger.info(f"OLX - página {pagina}: {itens_pagina} imóveis encontrados")
            if itens_pagina == 0:
                break

        browser.close()

    imoveis = list(coletados.values())
    if imoveis:
        _salvar_dados(imoveis, saida_json, saida_csv)
    return len(imoveis)


def _capturar_olx(url, saida_json, saida_csv, max_passos, headless):
    paginas = max(1, int(max_passos or 1))
    total = _capturar_olx_browser(url, saida_json, saida_csv, paginas, headless=headless)
    if total == 0 and headless:
        logger.info("OLX sem resultados em headless; tentando novamente com navegador visível.")
        total = _capturar_olx_browser(url, saida_json, saida_csv, paginas, headless=False)
    return total


def capturar_listings_renderizados(
    url: str,
    saida_json="imoveis_zap.json",
    saida_csv="imoveis_zap.csv",
    headless=True,
    sleep_ms=1200,
    max_loops=25,
) -> int:
    portal_key, portal = detectar_portal(url)
    logger.info(f"Iniciando captura: {url} ({portal['nome']})")

    if portal_key == "chavesnamao.com.br":
        return _capturar_chaves(url, saida_json, saida_csv, max_loops, headless=headless)
    if portal_key == "olx.com.br":
        return _capturar_olx(url, saida_json, saida_csv, max_loops, headless=headless)
    if portal_key == "vivareal.com.br":
        return _capturar_vivareal(url, saida_json, saida_csv, headless, sleep_ms, max_loops)
    return _capturar_zap(url, saida_json, saida_csv, headless, sleep_ms, max_loops)


def _salvar_dados(dados, json_path, csv_path):
    if not dados:
        return
    df = pd.DataFrame(dados)
    df.to_json(json_path, orient="records", force_ascii=False, indent=2)
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    logger.info(f"Dados salvos em {json_path} e {csv_path} com {len(dados)} registros.")
