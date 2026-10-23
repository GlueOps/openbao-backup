FROM openbao/openbao:2.7.0@sha256:71156a1c6623a5fa3f5e61b0c6a8ead0faf0df29a778339188443551995d1315
USER root
RUN apk add --no-cache python3
COPY baokv.py /app/baokv.py
# back to the base image's non-root user; the README's `--user` flag is
# still what makes dump files land owned by the operator
USER openbao
WORKDIR /work
ENTRYPOINT ["python3", "/app/baokv.py"]
