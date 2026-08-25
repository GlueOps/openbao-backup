FROM openbao/openbao:2.4.4@sha256:595c83b42614a4d2b044608e4593c05b019c5db25bc9c185d8fff3ac96c03ddd
USER root
RUN apk add --no-cache python3
COPY baokv.py /app/baokv.py
WORKDIR /work
ENTRYPOINT ["python3", "/app/baokv.py"]
