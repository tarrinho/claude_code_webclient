FROM python:3.13-slim

# Install dependencies
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt

# Create non-root user
RUN useradd -m -s /bin/bash appuser

# Copy application files
WORKDIR /app
COPY app.py auth.py config.py db.py runner.py claude_proxy.py ./
COPY web/ web/

# Create data and projects directories
RUN mkdir -p /data /projects && chown -R appuser:appuser /app /data /projects

# Make app a proper package so imports work from anywhere
RUN touch /app/__init__.py
ENV PYTHONPATH=/app

# Switch to non-root user
USER appuser

EXPOSE 8080

# Start FastAPI only. In proxy mode, claude_proxy.py runs separately on the host.
CMD ["sh", "-c", "uvicorn app:app --host ${WC_LISTEN_HOST:-0.0.0.0} --port ${WC_PORT:-8080}"]