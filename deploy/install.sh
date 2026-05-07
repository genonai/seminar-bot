#!/usr/bin/env bash
# 181 서버 (또는 다른 docker 가능한 머신) 초기 설치.
# 사용: ssh kube-server, 그 다음 bash /opt/seminar-bot/deploy/install.sh
#
# 전제조건:
#   - /opt/seminar-bot 에 코드가 있음 (git clone)
#   - /opt/seminar-bot/.env 가 채워져있음 (실제 토큰)
#   - docker + docker compose v2 (`docker compose` 명령) 설치됨

set -euo pipefail

REPO_DIR="/opt/seminar-bot"
cd "${REPO_DIR}"

if ! command -v docker >/dev/null 2>&1; then
  echo "❌ docker 미설치. company server에 docker 설치 권한이 있는지 확인 필요."
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "❌ 'docker compose' 플러그인 없음. docker compose v2 설치 필요."
  echo "   참고: https://docs.docker.com/compose/install/linux/"
  exit 1
fi

if [[ ! -f "${REPO_DIR}/.env" ]]; then
  echo "❌ ${REPO_DIR}/.env 없음. 토큰 채워 넣고 다시 실행."
  exit 1
fi

echo "▶ DB 영속 디렉토리 준비"
mkdir -p "${REPO_DIR}/data"

echo "▶ 이미지 빌드"
docker compose build

echo "▶ 컨테이너 시작 (백그라운드)"
docker compose up -d

sleep 2
echo "▶ 상태"
docker compose ps

echo ""
echo "✅ 설치 완료"
echo "로그 보기:    docker compose -f ${REPO_DIR}/docker-compose.yml logs -f"
echo "재시작:       docker compose -f ${REPO_DIR}/docker-compose.yml restart"
echo "정지:         docker compose -f ${REPO_DIR}/docker-compose.yml down"
echo "코드 갱신:    cd ${REPO_DIR} && git pull && docker compose up -d --build"
echo ""
echo "Schedule 시드는 별도로 (한 번):"
echo "  docker compose exec bot python scripts/seed_schedule.py --shuffle"
