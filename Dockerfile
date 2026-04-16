# Render / 기타 PaaS: Git 없이 Docker Hub 등 레지스트리에서 배포할 때 사용.
# 로컬: docker build --platform linux/amd64 -t <dockerhub-user>/saudi-pharma:latest .
# Render는 컨테이너 실행 시 PORT를 주입한다.

FROM python:3.12-slim-bookworm

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY requirements.txt .

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y build-essential \
    && apt-get autoremove -y --purge \
    && rm -rf /var/lib/apt/lists/*

COPY . .

EXPOSE 8000

CMD ["sh", "-c", "exec uvicorn frontend.server:app --host 0.0.0.0 --port ${PORT:-8000}"]
