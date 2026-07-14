FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Scan history lives here. docker-compose bind-mounts ./data over /data so the
# history survives image updates. Creating the directory here (and defaulting
# DATA_DIR to it) means the app still starts and works when run WITHOUT a
# volume — the history is just ephemeral in that case, not a crash.
ENV DATA_DIR=/data
RUN mkdir -p /data

COPY . .
EXPOSE 5000
CMD ["python", "app.py"]
