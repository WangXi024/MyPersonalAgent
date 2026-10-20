FROM python:3.12.15-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
RUN pip install --no-cache-dir uv==0.9.3 && useradd --uid 10001 --create-home app
WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY backend/ ./
RUN uv sync --frozen --no-dev && mkdir -p /data/attachments && chown app:app /data/attachments
COPY deploy/docker/app-entrypoint.py /usr/local/bin/app-entrypoint.py
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT ["python", "/usr/local/bin/app-entrypoint.py"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
