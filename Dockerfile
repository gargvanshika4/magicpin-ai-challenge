FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py bot.py conversation_handlers.py ./
COPY vera ./vera
EXPOSE 8080
# single worker on purpose: context + conversation state is in-process
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT} --workers 1"]
