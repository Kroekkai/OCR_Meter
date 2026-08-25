FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Bangkok

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts

RUN mkdir -p /data/images
VOLUME ["/data/images"]

EXPOSE 3003

# Single Python process on port 3003 only, as required.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3003"]
