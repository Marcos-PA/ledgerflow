FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
	PYTHONUNBUFFERED=1 \
	UV_PROJECT_ENVIRONMENT=/app/.venv \
	PATH="/app/.venv/bin:${PATH}"

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project --no-dev

COPY services/ services/

RUN uv sync --locked --no-dev

EXPOSE 8000

CMD ["uvicorn", "services.payments.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
