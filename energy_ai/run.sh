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

# Raspberry Pi 5 has four logical CPU cores. Keep native numerical libraries and
# loky/joblib workers to at most two so evaluation/model maintenance can use
# parallel native code without consuming all CPU capacity needed by Home
# Assistant, the planner, actuator and watchdog. These are upper bounds only;
# they do not pin work to specific cores or force algorithms to parallelize.
export LOKY_MAX_CPU_COUNT=2
export OMP_NUM_THREADS=2
export OPENBLAS_NUM_THREADS=2
export MKL_NUM_THREADS=2

exec /opt/energy-ai/venv/bin/uvicorn app.runtime_operator:app \
  --app-dir /opt/energy-ai \
  --host 0.0.0.0 \
  --port 8099 \
  --proxy-headers
