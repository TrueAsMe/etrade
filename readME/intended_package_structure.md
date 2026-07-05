# **Core Folder Structure and Pipeline Outline:**

## Core Folder Structure

* `config.py` — Load secrets from environment (never commit credentials)
* `data_feed.py` — OHLC pipeline (5m/1h/4h data fetching)
* `signal.py` — OTA zone detection + 6 odds enhancer scoring
* `alerts.py` — REST POST to TradersPost webhook
* `risk_manager.py` — Position sizing, daily limits, drawdown controls
* `app.py` — Main entry point (runs on Render via cron)

## Pipeline Outline

┌─────────────────────────────────────────────────────────────────────┐
│                         LOCAL DEVELOPMENT                            	│
├─────────────────────────────────────────────────────────────────────┤
│  Mac (/prod/em_algoVSCode)                                          			│
│  ├─ signal.py (OTA zone detection + scoring)                       		│
│  ├─ data_feed.py (5m/1h/4h OHLC pipeline)                         		│
│  ├─ alerts.py (REST POST to webhook)                               		│
│  ├─ risk_manager.py (position sizing, daily limits)                		│
│  ├─ config.yml (secrets: API keys, account IDs)                    		│
│  └─ requirements.txt (pandas, numpy, requests, python-dotenv) │
└─────────────────────────────────────────────────────────────────────┘
                                ↓
                        git push origin main
                                ↓
┌─────────────────────────────────────────────────────────────────────┐
│                    VERSION CONTROL (GITHUB)    			│
├─────────────────────────────────────────────────────────────────────┤
│  GitHub Repo: github.com/YOUR_GITHUB/em-algo-automation     │
│  ├─ Branch: main (production)                                       			│
│  ├─ Branch: dev (testing)                                           			│
│  ├─ .gitignore (NEVER commit secrets)                              		│
│  └─ README.md (deployment instructions)                            	│
└─────────────────────────────────────────────────────────────────────┘
                                ↓
                   Render detects push → redeploy
                                ↓
┌─────────────────────────────────────────────────────────────────────┐
│                    CLOUD EXECUTION (RENDER)                          		│
├─────────────────────────────────────────────────────────────────────┤
│  Render Background Worker (Python 3.11)                            		│
│  ├─ Runs: python app.py                                            			│
│  ├─ Trigger: Cron job (9:00 AM ET Mon-Fri)                        		│
│  ├─ Environment: Render secrets (API keys, tokens)               	│
│  ├─ Logs: Real-time streaming to Render dashboard                	│
│  └─ Pricing: Free tier ($0) or Hobby ($7/month)                  		│
│                                                                     				│
│  Process Flow:                                                      				│
│  1. Cron fires at 8:00 AM ET                                       				│
│  2. data_feed.py pulls latest 5m/1h/4h data                       		│
│  3. signal.py detects zones, scores setups                        		│
│  4. alerts.py POSTs JSON payload to TradersPost webhook           	│
│  5. Response logged, execution confirmed                           		│
└─────────────────────────────────────────────────────────────────────┘
                                ↓
                      REST API / Webhook POST
                     (JSON payload with signal)
                                ↓
┌─────────────────────────────────────────────────────────────────────┐
│                  ORDER ROUTING (TRADERSPOST) 			│
├─────────────────────────────────────────────────────────────────────┤
│  TradersPost Cloud (Free tier)                                      			│
│  ├─ Receives webhook POST from Render                             		│
│  ├─ Parses signal JSON (entry, stop, target, qty)                		│
│  ├─ Validates against Tradovate API schema                       		│
│  ├─ Routes to Tradovate Cloud API                                 			│
│  ├─ Logs all execution (viewable in TradersPost dashboard)        	│
│  └─ Confirms execution status back to Render (optional)           	│
└─────────────────────────────────────────────────────────────────────┘
                                ↓
                      Tradovate Cloud API
                                ↓
┌─────────────────────────────────────────────────────────────────────┐
│                    ACCOUNT EXECUTION (LUCID)                	│
├─────────────────────────────────────────────────────────────────────┤
│  Lucid Prop Firm Account (via Tradovate)                           		│
│  ├─ Receives bracket orders from TradersPost                      		│
│  ├─ Executes live on Lucid account                                			│
│  ├─ Daily P&L, balance, drawdown tracked                          		│
│  └─ Lucid rules enforced (position limits, daily loss, etc.)      	│
└─────────────────────────────────────────────────────────────────────┘
