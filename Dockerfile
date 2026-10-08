# One image for both the pipeline (`pitlake ingest`) and the API (`pitlake serve`).
FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir build && python -m build --wheel --outdir /dist

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PITLAKE_SAMPLE_DIR=/app/data/sample \
    PITLAKE_DATA_DIR=/data
WORKDIR /app
COPY --from=build /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl
COPY data/sample ./data/sample
# Run as an unprivileged user; /data is the only writable path (mount a volume or S3 there).
RUN useradd --create-home --uid 10001 pitlake && mkdir -p /data && chown pitlake /data
USER pitlake
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"
CMD ["pitlake", "serve", "--host", "0.0.0.0", "--port", "8000"]
