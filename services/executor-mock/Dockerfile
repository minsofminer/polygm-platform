# Stdlib only, on purpose: the thing that emulates the venue must not need a dependency install to start.
FROM python:3.12-slim
WORKDIR /srv/mock
COPY services/executor-mock /srv/mock
COPY packages /srv/packages
ENV PYTHONPATH=/srv/packages
# `-s /usr/sbin/nologin` (P14 D4): same reason as the API image — the process needs no shell, and
# `useradd -r` gives it /bin/sh by default.
RUN useradd -r -u 10002 -s /usr/sbin/nologin mock
USER mock
EXPOSE 8090
CMD ["python3", "mock_clob.py", "--host", "0.0.0.0", "--port", "8090"]
