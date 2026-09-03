FROM python:3.13-alpine
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY app.py /app/app.py
RUN addgroup -S monitor && adduser -S -G monitor monitor && mkdir -p /data && chown monitor:monitor /data
USER monitor
CMD ["python", "-m", "app"]
