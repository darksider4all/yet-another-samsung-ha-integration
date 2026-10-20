FROM python:3.11-slim
RUN pip install --no-cache-dir websocket-client==1.9.2
WORKDIR /app
COPY reader.py .
COPY app.wgt .
COPY img/ /app/img/
ENV PYTHONUNBUFFERED=1
EXPOSE 8777
CMD ["python", "reader.py"]