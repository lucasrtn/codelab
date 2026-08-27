FROM python:3.13-slim

WORKDIR /opt/codelab/app-manager

RUN pip install --no-cache-dir flask

COPY app.py .

EXPOSE 9001

CMD ["python", "app.py"]
