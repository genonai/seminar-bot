#!/usr/bin/env bash
# 로컬 → 181 서버 코드 동기화. .env는 동기화하지 않음 (서버에서 직접 관리).
# 사용: bash deploy/sync.sh

set -euo pipefail

SERVER="kube-server"               # ~/.ssh/config alias
REMOTE_DIR="/opt/seminar-bot"
LOCAL_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "▶ rsync (excludes .env, .venv, *.db, .git, __pycache__)"
rsync -av --delete \
  --exclude='.env' \
  --exclude='.venv/' \
  --exclude='*.db' \
  --exclude='*.db-journal' \
  --exclude='.git/' \
  --exclude='__pycache__/' \
  --exclude='.pytest_cache/' \
  --exclude='.ruff_cache/' \
  --exclude='*.egg-info/' \
  --exclude='bot.log' \
  "${LOCAL_DIR}/" "${SERVER}:${REMOTE_DIR}/"

echo ""
echo "✅ 동기화 완료"
echo ""
echo "다음 단계:"
echo "  1) ssh ${SERVER}"
echo "  2) (.env 처음이면 직접 채우기): sudo nano ${REMOTE_DIR}/.env"
echo "  3) 처음 설치: sudo bash ${REMOTE_DIR}/deploy/install.sh"
echo "     이미 설치돼있으면 재시작: sudo systemctl restart seminar-bot"
echo "  4) 로그 확인: sudo journalctl -u seminar-bot -f"
