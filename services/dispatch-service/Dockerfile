# Multi-stage build.
#
# Stage 1 installs the dependencies; stage 2 copies only the installed
# packages, not pip's build cache or compilers. Smaller image means faster
# deploys and less of the 500 MB ECR free-tier allowance consumed later.

# ---------- Stage 1: build dependencies ----------
FROM python:3.12-slim AS builder

WORKDIR /code

COPY requirements.txt .
# --user installs into /root/.local, which stage 2 copies out wholesale.
RUN pip install --no-cache-dir --user -r requirements.txt


# ---------- Stage 2: tests ----------
# Not part of the runtime image. Build and run it with:
#     docker build --target test -t fp-consignment-test .
#     docker run --rm fp-consignment-test
# Needs no database and no Redis -- see tests/test_waybills.py.
FROM python:3.12-slim AS test

WORKDIR /code
COPY --from=builder /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY ./app ./app
COPY ./tests ./tests
COPY pytest.ini .

CMD ["pytest", "-q"]


# ---------- Stage 3: runtime (the default build target) ----------
FROM python:3.12-slim

WORKDIR /code

# Do not run as root. If someone breaks in through the application they
# land as an unprivileged user with nothing to escalate to.
RUN useradd --create-home --shell /bin/bash appuser

# --chown so the files are owned by the user that will run them.
COPY --from=builder --chown=appuser:appuser /root/.local /home/appuser/.local
COPY --chown=appuser:appuser ./app ./app

ENV PATH=/home/appuser/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER appuser

EXPOSE 8000

# Docker reports this in `docker compose ps`, and Compose uses it to hold
# dependent containers back until this one is actually serving.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
