FROM python:3.13-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Runs as a non-root user.
RUN useradd -m karen && chown -R karen /app
USER karen

CMD ["python", "server.py"]
