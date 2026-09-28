FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m pip install --no-cache-dir .

RUN groupadd --system flux \
    && useradd --system --gid flux --home-dir /var/lib/flux flux \
    && mkdir -p /var/lib/flux/logs /workspace \
    && chown -R flux:flux /var/lib/flux /workspace

USER flux

EXPOSE 8765

CMD ["python", "-m", "harness_prototype.application_service", "--host", "0.0.0.0", "--port", "8765", "--workspace", "/workspace", "--allow-remote"]
