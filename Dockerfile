FROM python:3.12-slim
WORKDIR /app
COPY daily_brief.py .
RUN pip install --no-cache-dir anthropic
CMD ["python", "daily_brief.py"]
