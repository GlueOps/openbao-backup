FROM openbao/openbao:2.6.5@sha256:09f1eaaee351c329d75a5a89654d6e1ad4792998650c3cbf0c3513a76d9d9b6e
USER root
RUN apk add --no-cache python3
COPY baokv.py /app/baokv.py
# back to the base image's non-root user; the README's `--user` flag is
# still what makes dump files land owned by the operator
USER openbao
WORKDIR /work
ENTRYPOINT ["python3", "/app/baokv.py"]
