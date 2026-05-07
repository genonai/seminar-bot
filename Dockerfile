# 가벼운 이미지. 시스템 패키지 추가는 의도적으로 최소화.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Seoul

WORKDIR /app

# 의존성 먼저 (코드 변경에도 layer 캐시 활용)
COPY pyproject.toml ./
RUN pip install --upgrade pip wheel setuptools && \
    pip install .

# 코드 복사
COPY . .

# DB 마운트 포인트
RUN mkdir -p /var/lib/seminar-bot

COPY deploy/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "src.main"]
