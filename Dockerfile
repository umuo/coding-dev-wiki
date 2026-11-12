FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git ripgrep && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md requirements.lock ./
COPY app ./app
COPY web ./web
RUN pip install --no-cache-dir -c requirements.lock . && python -m app.runtime.install --destination /usr/local/bin/opencode && useradd --create-home wiki && mkdir /data && chown wiki:wiki /data
USER wiki
ENV WIKI_DATA_DIR=/data WIKI_ENV=production WIKI_DEV_MODE=0
EXPOSE 8080
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8080"]
