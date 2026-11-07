#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
test -f deployment.env
docker compose -p novideos config --quiet
docker compose -p novideos up --build -d
docker compose -p novideos ps
