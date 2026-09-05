FROM python:3.11-slim
WORKDIR /app
COPY server_requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py ./
EXPOSE 3000
CMD ["python", "main.py"]
