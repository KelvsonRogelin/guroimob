from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

def extrair_imoveis_com_playwright(url: str):
    print("[ZAP] 🔍 Iniciando extração com Playwright...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=200)
        page = browser.new_page()
        print(f"[ZAP] 🌐 Abrindo URL: {url}")
        page.goto(url)

        print("[ZAP] ⏳ Esperando carregamento dos anúncios (10s)...")
        page.wait_for_timeout(10000)

        print("[ZAP] 🔄 Rolando a página para carregar mais anúncios...")
        for _ in range(5):
            page.mouse.wheel(0, 10000)
            page.wait_for_timeout(2000)

        html = page.content()

        with open("pagina_zap_debug.html", "w", encoding="utf-8") as f:
            f.write(html)
            print("[ZAP] ✅ HTML salvo em 'pagina_zap_debug.html'.")

        browser.close()

    print("[ZAP] 🔍 Carregando HTML manual para análise...")
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select("div.property-card__container")

    print(f"[ZAP] 🏘️ {len(cards)} imóveis encontrados.")

    imoveis = []
    for card in cards:
        try:
            titulo = card.select_one("span.property-card__title")
            endereco = card.select_one("span.property-card__address")
            preco = card.select_one("div.property-card__price")
            area = card.select_one("li.property-card__detail-area")
            quartos = card.select_one("li.property-card__detail-room")
            banheiros = card.select_one("li.property-card__detail-bathroom")
            vagas = card.select_one("li.property-card__detail-garage")
            link_tag = card.select_one("a.property-card__content-link")
            link = link_tag["href"] if link_tag else ""

            imoveis.append({
                "titulo": titulo.text.strip() if titulo else "",
                "endereco": endereco.text.strip() if endereco else "",
                "preco": preco.text.strip() if preco else "",
                "area": area.text.strip() if area else "",
                "quartos": quartos.text.strip() if quartos else "",
                "banheiros": banheiros.text.strip() if banheiros else "",
                "vagas": vagas.text.strip() if vagas else "",
                "link": f"https://www.zapimoveis.com.br{link}" if link and not link.startswith("http") else link
            })

        except Exception as e:
            print(f"[ZAP] ⚠️ Erro ao extrair um imóvel: {e}")

    return imoveis
