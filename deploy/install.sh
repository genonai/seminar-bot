#!/usr/bin/env bash
# 181 서버 초기 설치 스크립트.
# 사용: ssh kube-server, 그 다음 sudo bash /opt/seminar-bot/deploy/install.sh
#
# 전제조건:
#   - /opt/seminar-bot 에 코드가 이미 들어있음 (git clone 또는 rsync)
#   - /opt/seminar-bot/.env 가 채워져있음 (실제 토큰)
#   - python3.11 (또는 python3.12) 설치됨
#
# 환경변수 PYTHON 으로 인터프리터 override 가능:
#   sudo PYTHON=/usr/bin/python3.11 bash deploy/install.sh

set -euo pipefail

REPO_DIR="/opt/seminar-bot"
DATA_DIR="/var/lib/seminar-bot"
LOG_DIR="/var/log/seminar-bot"
SERVICE_NAME="seminar-bot"

# Python 인터프리터 자동 탐지 (3.11 우선)
if [[ -z "${PYTHON:-}" ]]; then
  for cand in python3.13 python3.12 python3.11 python3; do
    if command -v "$cand" >/dev/null 2>&1; then
      ver=$("$cand" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
      major=${ver%.*}; minor=${ver#*.}
      if [[ "$major" -ge 3 ]] && [[ "$minor" -ge 11 ]]; then
        PYTHON=$(command -v "$cand")
        break
      fi
    fi
  done
fi
if [[ -z "${PYTHON:-}" ]]; then
  echo "❌ python3.11+ 를 찾지 못함. 'sudo dnf install -y python3.11' 후 재시도."
  exit 1
fi
echo "▶ Python: $PYTHON ($($PYTHON --version))"

if [[ "${EUID}" -ne 0 ]]; then
  echo "❌ root로 실행해야 함: sudo bash $0"
  exit 1
fi

echo "▶ 디렉토리 준비"
mkdir -p "${DATA_DIR}" "${LOG_DIR}"
chown -R kube:kube "${DATA_DIR}" "${LOG_DIR}" "${REPO_DIR}"

echo "▶ Python venv + 의존성 (기존 .venv 있으면 제거)"
cd "${REPO_DIR}"
rm -rf .venv
sudo -u kube "$PYTHON" -m venv .venv
sudo -u kube .venv/bin/pip install --upgrade pip wheel setuptools
sudo -u kube .venv/bin/pip install -e .

echo "▶ DB 초기화 (멱등)"
sudo -u kube .venv/bin/python scripts/init_db.py

echo "▶ 멤버 시드 (멱등)"
sudo -u kube .venv/bin/python scripts/seed_members.py

if [[ ! -f "${DATA_DIR}/seminar.db" ]] || [[ -z "$(sudo -u kube .venv/bin/python -c "
from src.config import DB_PATH
from src.db import session
with session(DB_PATH) as conn:
    print(conn.execute('SELECT COUNT(*) FROM schedule').fetchone()[0])
")" ]]; then
  echo "ℹ️  schedule이 비어있다면 다음 명령으로 5주 사이클 시드:"
  echo "    sudo -u kube ${REPO_DIR}/.venv/bin/python ${REPO_DIR}/scripts/seed_schedule.py --shuffle"
fi

echo "▶ systemd unit 설치"
cp "${REPO_DIR}/deploy/seminar-bot.service" "/etc/systemd/system/${SERVICE_NAME}.service"
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"

sleep 2
echo "▶ 상태 확인"
systemctl status "${SERVICE_NAME}" --no-pager -l | head -20 || true

echo ""
echo "✅ 설치 완료"
echo "로그 보기:    sudo journalctl -u ${SERVICE_NAME} -f"
echo "재시작:       sudo systemctl restart ${SERVICE_NAME}"
echo "정지:         sudo systemctl stop ${SERVICE_NAME}"
