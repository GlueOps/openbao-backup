FROM openbao/openbao:2.4.4
USER root
RUN apk add --no-cache python3
COPY baokv.py /app/baokv.py
WORKDIR /work
ENTRYPOINT ["python3", "/app/baokv.py"]
