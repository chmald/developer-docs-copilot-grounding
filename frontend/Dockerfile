FROM python:3.12-slim
WORKDIR /app
COPY frontend/requirements.txt /app/frontend/requirements.txt
RUN pip install --no-cache-dir -r /app/frontend/requirements.txt \
    && useradd --create-home appuser
COPY frontend/app.py frontend/__init__.py /app/frontend/
COPY frontend/static /app/frontend/static
COPY scripts/mcp_fallback_server.py /app/scripts/mcp_fallback_server.py
COPY scripts/foundry_iq.py /app/scripts/foundry_iq.py
USER appuser
ENV FRONTEND_HOST=0.0.0.0
EXPOSE 8000
CMD ["python", "-m", "frontend.app"]
