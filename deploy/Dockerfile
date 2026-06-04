# Containerized unattended service. Run with: docker run --restart=always --env-file .env <image>
FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Paper by default; live requires .env flags + keys AND the edge-proven gate.
CMD ["python", "main.py", "service"]
