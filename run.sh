#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
echo "[SWTS] 의존성 설치..."
pip install -r requirements.txt
export SWTS_ROOT="$(pwd)/example/ecu_powertrain"
echo "[SWTS] 브라우저에서 http://localhost:5000 열기"
python app.py
