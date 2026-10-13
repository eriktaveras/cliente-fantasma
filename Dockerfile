FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY fantasma ./fantasma
COPY n8n ./n8n
COPY rubros ./rubros
COPY config.example.yaml ./config.yaml
RUN uv sync --frozen --no-dev

# runs/ es un bind mount del host: con el uid del dueño del directorio (1000 en el Beelink) el contenedor puede escribir sin root.
ARG UID=1000
RUN useradd --create-home --uid ${UID} fantasma && mkdir -p /app/runs && chown -R fantasma:fantasma /app/runs
USER fantasma
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
CMD ["uvicorn", "fantasma.app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
