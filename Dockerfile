FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY provenance ./provenance
COPY pyproject.toml ./

ENV PYTHONUNBUFFERED=1

