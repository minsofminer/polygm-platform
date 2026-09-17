# Stdlib only, on purpose: the thing that emulates the venue must not need a dependency install to start.
FROM python:3.12-slim
WORKDIR /srv/mock
COPY services/executor-mock /srv/mock
COPY packages /srv/packages
ENV PYTHONPATH=/srv/packages
RUN useradd -r -u 10002 mock
USER mock
EXPOSE 8090
CMD ["python3", "mock_clob.py", "--host", "0.0.0.0", "--port", "8090"]
