#!/bin/zsh
cd "$(dirname "$0")"
open http://localhost:8787
exec python3 server.py
