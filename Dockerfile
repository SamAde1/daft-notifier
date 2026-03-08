FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

RUN groupadd --system app && useradd --system --gid app --create-home app

COPY daft_monitor /app/daft_monitor

# Create data and logs directories owned by the app user so mounted
# volumes inherit correct permissions on first run.
RUN mkdir -p /app/data /app/logs && chown -R app:app /app/data /app/logs

USER app

CMD ["python", "-m", "daft_monitor"]

