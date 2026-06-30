FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV BO_VPN_WORKER_HOST=0.0.0.0
ENV BO_VPN_WORKER_PORT=8000

WORKDIR /app

COPY pyproject.toml README.md ./
COPY bo_vpn_agent ./bo_vpn_agent

RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir .

EXPOSE 8000

CMD ["python", "-m", "bo_vpn_agent"]
