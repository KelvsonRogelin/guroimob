import argparse
import logging
from zap_imoveis import capturar_listings_renderizados
from filter import filtrar_por_m2

# Configuração de Logs
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

def main():
    parser = argparse.ArgumentParser(description="Captura imóveis do Zap e filtra por valor/m²")
    parser.add_argument("--url", type=str,
                        default="https://www.zapimoveis.com.br/venda/apartamentos/sc+florianopolis++centro/2-quartos/?transacao=venda&onde=%2CSanta+Catarina%2CFlorian%C3%B3polis%2C%2CCentro%2C%2C%2Cneighborhood%2CBR%3ESanta+Catarina%3ENULL%3EFlorianopolis%3EBarrios%3ECentro%2C-27.578449%2C-48.536231%2C%3B%2CSanta+Catarina%2CFlorian%C3%B3polis%2C%2CEstreito%2C%2C%2Cneighborhood%2CBR%3ESanta+Catarina%3ENULL%3EFlorianopolis%3EBarrios%3EEstreito%2C-27.592206%2C-48.577521%2C%3B%2CSanta+Catarina%2CFlorian%C3%B3polis%2C%2CItacorubi%2C%2C%2Cneighborhood%2CBR%3ESanta+Catarina%3ENULL%3EFlorianopolis%3EBarrios%3EItacorubi%2C-27.580226%2C-48.504667%2C&tipos=apartamento_residencial%2Ccasa_residencial%2Ccobertura_residencial&quartos=2%2C3%2C4&vagas=1%2C2%2C3%2C4",
                        help="URL de busca do Zap Imóveis")
    parser.add_argument("--valor-m2", type=float, default=None,
                        help="Valor máximo de R$/m² (tudo <= vai ser mantido). Ex: 8500")
    parser.add_argument("--scrolls", type=int, default=25,
                        help="Quantidade de rolagens (scrolls)")
    parser.add_argument("--sleep-ms", type=int, default=1200,
                        help="Tempo de espera entre rolagens em ms")
    parser.add_argument("--no-headless", action="store_true",
                        help="Exibe o navegador durante a captura")
    parser.add_argument("--keep-debug-html", action="store_true",
                        help="Mantém o arquivo pagina_zap_debug.html")

    args = parser.parse_args()

    logger.info("Iniciando captura (HTML renderizado)...")

    total = capturar_listings_renderizados(
        url=args.url,
        saida_json="imoveis_zap.json",
        saida_csv="imoveis_zap.csv",
        headless=not args.no_headless,
        sleep_ms=args.sleep_ms,
        max_loops=args.scrolls
    )

    if total == 0:
        logger.warning("Nenhum imóvel capturado.")
        return

    # -------------------------------
    # Chama o filtro logo em seguida
    # -------------------------------
    logger.info("Aplicando filtro de valor/m²...")
    filtrar_por_m2(
        csv_entrada="imoveis_zap.csv",
        csv_saida="imoveis_filtrados.csv",
        valor_maximo_m2=args.valor_m2,
    )

    logger.info("Finalizado.")


if __name__ == "__main__":
    main()
