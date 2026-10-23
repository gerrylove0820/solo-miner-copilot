FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    CONFIG_PATH=/config/config.yaml DATA_DIR=/data TZ=UTC
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
VOLUME ["/data"]
CMD ["python", "-m", "app.main"]
