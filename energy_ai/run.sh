#!/usr/bin/env sh
set -eu

OPTIONS=/data/options.json

OPENAI_API_KEY="$(python3 -c 'import json; print(json.load(open("/data/options.json")).get("openai_api_key",""))')"
OPENAI_MODEL="$(python3 -c 'import json; print(json.load(open("/data/options.json")).get("openai_model","gpt-5.6-luna"))')"

export OPENAI_API_KEY
export OPENAI_MODEL
export ENERGY_AI_DB=/data/energy_ai.db

exec /opt/energy-ai/venv/bin/uvicorn app.main:app \
  --app-dir /opt/energy-ai \
  --host 0.0.0.0 \
  --port 8099 \
  --proxy-headers
