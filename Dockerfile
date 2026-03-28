FROM python:3.11-slim

WORKDIR /app

COPY requirements_prod.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/data

ENV PYTHONUNBUFFERED=1
ENV STATE_FILE=/app/data/state.json
ENV LOG_FILE=/app/data/hedge_log.json

# Para simulación: CMD python app.py
# Para producción: CMD python app_prod.py
CMD ["python", "app_prod.py"]

EXPOSE 8080
