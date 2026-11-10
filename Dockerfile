# Hirify-бот: лёгкий образ без сборочных зависимостей.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY main.py .

# непривилегированный пользователь; данные (SQLite) пишет в /app/data
RUN useradd -m bot && mkdir -p /app/data && chown -R bot:bot /app
USER bot

# VOLUME объявлен в docker-compose; тут только команда
CMD ["python", "main.py"]
