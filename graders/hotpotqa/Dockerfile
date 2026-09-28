FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 https://github.com/hotpotqa/hotpot.git /opt/hotpot
COPY grade.py /grader/grade.py
ENTRYPOINT []
