FROM python:3.12-slim-bookworm

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --no-create-home --uid 10001 customs \
    && install -d -o customs -g customs /data
COPY game.py .

USER customs
CMD ["python", "game.py", "--host", "0.0.0.0", "--data", "/data/customs.db"]
