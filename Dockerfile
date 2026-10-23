FROM openbao/openbao:2.6.3@sha256:a60afafda36337abe833c4a63894bf1095098f29abea4091e7e555a33dd52889
USER root
RUN apk add --no-cache python3
COPY baokv.py /app/baokv.py
# back to the base image's non-root user; the README's `--user` flag is
# still what makes dump files land owned by the operator
USER openbao
WORKDIR /work
ENTRYPOINT ["python3", "/app/baokv.py"]
