FROM python@sha256:ddb0207ae1f0356c2b724d740769b0c5f5f51cc54a0525178f721825f78fe74c

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements-qualification-linux-py312.lock ./
RUN python -m pip install --no-cache-dir --require-hashes --only-binary=:all: -r requirements-qualification-linux-py312.lock \
    && python -m pip check
COPY . .
RUN useradd --create-home --uid 10001 atelier \
    && mkdir -p /app/runtime/static \
    && chown -R atelier:atelier /app/runtime
USER atelier
EXPOSE 8000
CMD ["sh", "-c", "python manage.py migrate --noinput && python manage.py collectstatic --noinput --clear && exec gunicorn dv_project.wsgi:application --bind 0.0.0.0:8000 --workers 1 --threads 4 --timeout 90"]
