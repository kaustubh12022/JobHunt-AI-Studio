#!/usr/bin/env bash
set -e

echo "==================================================="
echo "          Launching JobHunt AI Studio"
echo "==================================================="
echo ""

if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
fi

python3 -m streamlit run app.py --server.port=8501 --server.address=0.0.0.0
