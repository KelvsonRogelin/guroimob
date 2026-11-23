# Guru Imob

Monitor de preços de imóveis que acompanha o **valor por metro quadrado (R$/m²)** dos anúncios publicados em vários portais, guarda o histórico de variação e emite alertas quando o preço cai abaixo do teto definido para cada imóvel.

Aplicação web em **Flask** (multiusuário), com captura de anúncios via **Playwright + BeautifulSoup** e persistência em **SQLite**.

---

## Principais recursos

- **Captura de anúncios** em vários portais a partir de uma URL de busca.
- **Cálculo automático de R$/m²** (preço ÷ área) e filtro por teto de valor.
- **Histórico de preços** por imóvel, com detecção de queda e variação.
- **Alertas por teto**: define-se um limite de R$/m² por imóvel; quando o anúncio fica igual ou abaixo dele, é gerado um alerta.
- **Painel multiusuário**: cada conta enxerga apenas os seus imóveis (isolamento por usuário).
- **Exportação** para CSV e para o Google Sheets.
- **Página de portais** com os sites monitorados.
- **Suporte in-app**: reportar bug, pedir melhoria ou sugerir um novo site, com envio por e-mail (SMTP).

## Portais suportados

| Portal | Domínio | Como a página é lida |
|---|---|---|
| ZAP Imóveis | `zapimoveis.com.br` | Carrega mais ao rolar |
| Viva Real | `vivareal.com.br` | Carrega mais ao rolar |
| OLX | `olx.com.br` | Dividido em páginas |
| Chaves na Mão | `chavesnamao.com.br` | Dividido em páginas |

## Requisitos

- Python 3.11 ou superior
- Windows, Linux ou macOS

## Instalação

```bash
# 1) Clone o repositório
git clone https://github.com/KelvsonRogelin/guroimob.git
cd guroimob

# 2) Crie e ative o ambiente virtual
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate

# 3) Instale as dependências
pip install -r requirements.txt

# 4) Instale o navegador usado na captura
playwright install chromium
```

## Configuração

O projeto lê as configurações de um arquivo `.env` na raiz (que **não** é versionado). Copie o modelo e preencha:

```bash
copy .env.example .env      # Windows
cp .env.example .env        # Linux / macOS
```

| Variável | Descrição |
|---|---|
| `APP_HOST` | Endereço de escuta: `127.0.0.1` (só esta máquina) ou `0.0.0.0` (rede local) |
| `APP_PORT` | Porta do servidor (padrão `5000`) |
| `FLASK_SECRET` | Chave de assinatura da sessão |
| `ADMIN_EMAIL` / `ADMIN_PASS` | Conta administradora criada no primeiro start |
| `CIDADE`, `ESTADO`, `BAIRROS`, `TIPO_IMOVEL` | Filtros padrão da captura |
| `MIN_QUARTOS`, `MIN_AREA`, `PRECO_LIMITE_M2` | Filtros padrão de busca |
| `GOOGLE_SHEET_ID` / `SHEET_NAME` | Destino da exportação para o Google Sheets |
| `GOOGLE_CREDENTIALS_FILE` | Caminho do JSON da conta de serviço do Google |
| `WHATSAPP_TOKEN` / `WHATSAPP_PHONE_ID` | Credenciais da WhatsApp Cloud API |
| `WHATSAPP_GRAPH_VERSION` | Versão da Graph API (padrão `v21.0`) |
| `SUPPORT_EMAIL` | E-mail que recebe os pedidos de suporte |
| `SMTP_HOST` / `SMTP_PORT` | Servidor SMTP (ex.: `smtp.gmail.com` / `587`) |
| `SMTP_USER` / `SMTP_PASS` | Login SMTP e senha de app |
| `SMTP_FROM` | Remetente exibido (opcional) |
| `AUTH_USER` / `AUTH_PASS` | Credenciais legadas (compatibilidade) |

> As credenciais de WhatsApp, e-mail e Google são **opcionais**. Sem elas, o painel funciona normalmente — apenas o envio de alertas/e-mails fica desativado.

## Como rodar

```bash
python app.py
```

Por padrão, sobe em `http://127.0.0.1:5000`. Faça login com a conta administradora (`ADMIN_EMAIL` / `ADMIN_PASS`) ou crie uma conta em `/cadastro`.

### Captura pela linha de comando (opcional)

```bash
python main.py --url "https://www.zapimoveis.com.br/venda/..." --valor-m2 8500 --scrolls 25
```

## Acesso pela rede local

Para permitir que outra pessoa (por exemplo, um corretor) acesse pelo Wi-Fi:

1. No `.env`, defina `APP_HOST=0.0.0.0`.
2. Libere a porta no firewall do Windows (executar como administrador):

```powershell
New-NetFirewallRule -DisplayName "Guru Imob (LAN)" -Direction Inbound `
  -Action Allow -Protocol TCP -LocalPort 5000 -RemoteAddress LocalSubnet -Profile Any
```

3. Descubra o IP local com `ipconfig` e acesse de outra máquina: `http://SEU_IP:5000`.

A regra acima libera apenas os IPs da própria rede local (`LocalSubnet`).

## Estrutura do projeto

```
guroimob/
├── app.py                              # Servidor Flask (rotas e API)
├── db.py                               # Persistência (SQLite): imóveis, usuários, suporte
├── zap_imoveis.py                      # Captura nos portais (Playwright)
├── filter.py                           # Parsing de preço/área e filtros de R$/m²
├── notificar.py                        # Alertas (WhatsApp) e e-mail (SMTP)
├── sheets.py                           # Exportação para o Google Sheets
├── config.py                           # Configurações gerais
├── main.py                             # Captura via linha de comando
├── extrair_imoveis_com_playwright.py   # Extração auxiliar de anúncios
├── index.html                          # Painel principal
├── login.html / cadastro.html          # Autenticação
├── portais.html                        # Página dos portais monitorados
├── logo/                               # Identidade visual
└── requirements.txt
```

## Segurança

Não versionados (veja o `.gitignore`): `.env`, a pasta `credenciais/`, o banco `*.db`, os CSV/JSON de dados gerados e os logs. Preencha o `.env` localmente e **nunca** faça commit de segredos.

---

Projeto de uso interno.
