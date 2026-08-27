#!/usr/bin/env sh
set -eu

OPENAI_API_KEY="$(python3 -c 'import json; print(json.load(open("/data/options.json")).get("openai_api_key",""))')"
OPENAI_MODEL="$(python3 -c 'import json; print(json.load(open("/data/options.json")).get("openai_model","gpt-5.6-luna"))')"
HA_ACCESS_TOKEN="$(python3 -c 'import json; print(json.load(open("/data/options.json")).get("ha_access_token",""))')"
HA_BASE_URL="$(python3 -c 'import json; print(json.load(open("/data/options.json")).get("ha_base_url","http://homeassistant:8123/api"))')"

export OPENAI_API_KEY
export OPENAI_MODEL
export HA_ACCESS_TOKEN
export HA_BASE_URL
export ENERGY_AI_DB=/data/energy_ai.db

exec /opt/energy-ai/venv/bin/uvicorn app.runtime_entry_v177:app \
  --app-dir /opt/energy-ai \
  --host 0.0.0.0 \
  --port 8099 \
  --proxy-headers
