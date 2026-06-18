FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY ceai ./ceai
COPY migrations ./migrations
COPY tests ./tests

RUN pip install --no-cache-dir .

CMD ["python", "-m", "ceai.main"]
