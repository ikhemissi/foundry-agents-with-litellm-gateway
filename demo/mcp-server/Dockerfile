FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py catalogue.py main.py ./
COPY curriculum ./curriculum

RUN useradd --system --uid 10001 --no-create-home app
USER 10001

EXPOSE 8080
CMD ["python", "main.py"]
