FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade "pip>=26.0" \
    && pip install --no-cache-dir -r requirements.txt

COPY . .

# Ensure the persistent data directory exists (for arbiter_state.json:
# users, custom providers, model enable/disable flags)
RUN mkdir -p /app/data

# Run as non-root user for security
RUN adduser --disabled-password --gecos "" appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
