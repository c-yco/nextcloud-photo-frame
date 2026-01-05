FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Entry point is overridden in docker-compose for the worker
CMD ["gunicorn", "-w", "4", "-b", "0.0.0.0:5000", "app:app"]
