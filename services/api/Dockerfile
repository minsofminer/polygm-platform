# P04 API image. Deliberately no `latest`, and no build-time network beyond pip installs: the deploy that
# happens at 3am must be reproducible from the commit, not from a registry's mood.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/srv/app/packages:/srv/app/services/api
WORKDIR /srv/app

COPY requirements.txt /srv/app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY packages /srv/app/packages
COPY services/api /srv/app/services/api
COPY db /srv/app/db
COPY brand /srv/app/brand
COPY tools /srv/app/tools

# Runs as non-root; binds 0.0.0.0 because compose maps it to 127.0.0.1 on the host.
RUN useradd -r -u 10001 polygm && mkdir -p /srv/app/var && chown -R polygm /srv/app
USER polygm
EXPOSE 8080
HEALTHCHECK --interval=5s --timeout=3s --retries=5 \
  CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=2).status==200 else 1)"
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
