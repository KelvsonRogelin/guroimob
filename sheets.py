import os
import logging
import pandas as pd
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from typing import Optional

# Configuração de Logs
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# Configurações Padrão
DEFAULT_CSV_FILTRADO = "imoveis_filtrados.csv"
DEFAULT_SHEET_NAME = "Imoveis Filtrados"
# Tenta localizar credenciais automaticamente
DEFAULT_CREDENTIALS_FILE = "credentials.json"
CREDENTIALS_DIR = "credenciais"

def _encontrar_credenciais() -> Optional[str]:
    """Tenta encontrar o arquivo de credenciais JSON."""
    # 1. Verifica se foi definido via variável de ambiente
    env_creds = os.getenv("GOOGLE_SHEETS_CREDENTIALS")
    if env_creds and os.path.exists(env_creds):
        return env_creds

    # 2. Verifica no diretório atual
    if os.path.exists(DEFAULT_CREDENTIALS_FILE):
        return DEFAULT_CREDENTIALS_FILE
    
    # 3. Verifica no diretório 'credenciais'
    if os.path.exists(CREDENTIALS_DIR):
        for f in os.listdir(CREDENTIALS_DIR):
            if f.endswith(".json"):
                return os.path.join(CREDENTIALS_DIR, f)
    
    return None

def exportar_para_sheets(
    csv_path: str = DEFAULT_CSV_FILTRADO,
    sheet_name: str = DEFAULT_SHEET_NAME,
    credentials_path: Optional[str] = None
) -> bool:
    logger.info(f"Iniciando exportação para Google Sheets: {sheet_name}")
    
    # Verifica arquivo CSV
    if not os.path.exists(csv_path):
        logger.error(f"Arquivo CSV não encontrado: {csv_path}")
        return False
        
    try:
        df = pd.read_csv(csv_path)
        logger.info(f"Lido CSV com {len(df)} linhas.")
    except Exception as e:
        logger.error(f"Erro ao ler CSV: {e}")
        return False

    if df.empty:
        logger.warning("CSV vazio. Nada a exportar.")
        return False

    # Resolve credenciais
    creds_file = credentials_path or _encontrar_credenciais()
    if not creds_file:
        logger.error("Arquivo de credenciais do Google Sheets não encontrado.")
        logger.info("Coloque o JSON de credenciais na pasta 'credenciais/' ou defina GOOGLE_SHEETS_CREDENTIALS.")
        return False
        
    logger.info(f"Usando credenciais: {creds_file}")

    # Autenticação
    scope = ["https://spreadsheets.google.com/feeds",
             "https://www.googleapis.com/auth/drive"]
    
    try:
        creds = ServiceAccountCredentials.from_json_keyfile_name(creds_file, scope)
        client = gspread.authorize(creds)
    except Exception as e:
        logger.error(f"Erro na autenticação com Google Sheets: {e}")
        return False

    # Abre (ou cria) planilha
    try:
        try:
            sh = client.open(sheet_name)
            logger.info(f"Planilha '{sheet_name}' encontrada.")
        except gspread.SpreadsheetNotFound:
            sh = client.create(sheet_name)
            sh.share(None, perm_type="anyone", role="writer")
            logger.info(f"Planilha '{sheet_name}' criada e liberada para escrita.")
        
        worksheet = sh.sheet1
        worksheet.clear()
        
        # Prepara dados (converte NaN para string vazia ou similar, pois JSON não suporta NaN)
        df = df.fillna("")
        
        # Escreve cabeçalho + dados
        data = [df.columns.values.tolist()] + df.values.tolist()
        worksheet.update(data)
        logger.info(f"Sucesso! {len(df)} linhas exportadas.")
        return True
        
    except Exception as e:
        logger.error(f"Erro ao manipular planilha: {e}")
        return False

if __name__ == "__main__":
    exportar_para_sheets()
