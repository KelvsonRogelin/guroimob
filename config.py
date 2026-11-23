import os
from dotenv import load_dotenv

load_dotenv()

CIDADE = os.getenv("CIDADE", "joinville")
ESTADO = os.getenv("ESTADO", "sc")
TIPO_IMOVEL = os.getenv("TIPO_IMOVEL", "apartamentos")
PRECO_REFERENCIA_M2 = float(os.getenv("PRECO_REFERENCIA_M2", 6000))

GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
PLANILHA_NOME = os.getenv("PLANILHA_NOME")
GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE")
