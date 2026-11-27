FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Dua user tanpa hak akses untuk menjalankan kode peserta (bot A dan bot B)
RUN useradd --system --no-create-home --uid 10001 --shell /usr/sbin/nologin bot_a \
 && useradd --system --no-create-home --uid 10002 --shell /usr/sbin/nologin bot_b

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY engine.py worker.py bot_runner.py ./
COPY bots/ ./bots/

CMD ["python", "worker.py"]
