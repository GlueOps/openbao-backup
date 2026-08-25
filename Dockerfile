FROM openbao/openbao:2.6.1@sha256:5b2486ab0fb90bbc788cc345b0a08616dfb375873ee8be5df3a2fd4d378a67e0
USER root
RUN apk add --no-cache python3
COPY baokv.py /app/baokv.py
WORKDIR /work
ENTRYPOINT ["python3", "/app/baokv.py"]
