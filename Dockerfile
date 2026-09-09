FROM python:3.11-slim

WORKDIR /app
COPY requirements-web.txt .
RUN pip install --no-cache-dir -r requirements-web.txt
COPY server.py realtime_config.py weather.py knowledge.py ./
COPY static ./static

ENV PORT=8000
EXPOSE 8000
CMD ["python", "server.py"]
