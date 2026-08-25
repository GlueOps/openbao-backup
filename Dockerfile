FROM openbao/openbao:2.5.5@sha256:6150c4a6b62067db6141c8da7a6a6b5763f4f47c315343d0c848b40fecdfd452
USER root
RUN apk add --no-cache python3
COPY baokv.py /app/baokv.py
WORKDIR /work
ENTRYPOINT ["python3", "/app/baokv.py"]
