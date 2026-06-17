#!/bin/bash
set -e

# App interno (porta 8001): só /internal/submit, acessível apenas pela rede
# interna do docker-compose (não exposto ao host nem ao Cloudflare Tunnel).
uvicorn internal:app --host 0.0.0.0 --port 8001 --workers 1 &

# Painel do DJ + API de gestão (porta 8501): HTML estático + worker de
# download. Exposto via Cloudflare Tunnel, protegido por Cloudflare Access.
exec uvicorn api:app --host 0.0.0.0 --port 8501 --workers 1
