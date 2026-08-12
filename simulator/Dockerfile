# Lets the simulator run without Python installed on your machine:
#     docker compose --profile sim run --rm simulator --parcels 20
FROM python:3.12-slim

WORKDIR /sim

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY simulate_delhivery_scans.py .

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "simulate_delhivery_scans.py"]
