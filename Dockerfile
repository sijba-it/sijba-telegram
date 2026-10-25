FROM docker.io/library/python:3.11-slim

WORKDIR /app
COPY main.py .
# Read by sijba-provisioner-k8s at its build time (order form + defaults).
COPY connector.json /connector.json

EXPOSE 9102
CMD ["python3", "main.py"]
