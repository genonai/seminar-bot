#!/bin/sh
# Container 시작 시: DB 스키마 생성 + 멤버 시드 (둘 다 멱등) → 봇 실행
set -eu

echo "[entrypoint] init_db..."
python scripts/init_db.py

echo "[entrypoint] seed_members..."
python scripts/seed_members.py

echo "[entrypoint] starting bot: $@"
exec "$@"
