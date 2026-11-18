FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

ARG GIT_COMMIT=unknown
ENV GIT_COMMIT=${GIT_COMMIT}

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY config.py database.py schema.sql ./
COPY config ./config
COPY main ./main
COPY tests ./tests

CMD ["python", "-m", "main"]
