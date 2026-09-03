FROM python:3.12-alpine

WORKDIR /app
RUN mkdir -p /app/data
VOLUME /app/data
COPY sync.py .

CMD ["python", "-u", "sync.py"]
