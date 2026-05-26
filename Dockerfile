FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080

# Запуск из корня проекта, чтобы пути к шаблонам и конфигу были верными
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
