FROM openbao/openbao:2.7.1@sha256:6d2b93856e3fcf7b18ad855a0b51eaba474dc8b79cf554379ea32034797d2acf
USER root
RUN apk add --no-cache python3
COPY baokv.py /app/baokv.py
# back to the base image's non-root user; the README's `--user` flag is
# still what makes dump files land owned by the operator
USER openbao
WORKDIR /work
ENTRYPOINT ["python3", "/app/baokv.py"]
