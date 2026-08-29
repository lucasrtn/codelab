FROM python:3.13-slim 

WORKDIR /opt/codelab/app-manager 

ENV PYTHONUNBUFFERED=1 
ENV PYTHONDONTWRITEBYTECODE=1 

RUN pip install --no-cache-dir \ flask \ requests 

COPY app-manager/app.py /opt/codelab/app-manager/app.py 

EXPOSE 9001 

CMD ["python", "/opt/codelab/app-manager/app.py"]
