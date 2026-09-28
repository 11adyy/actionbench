FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir ujson
RUN git init /opt/hotpot && cd /opt/hotpot && git remote add origin https://github.com/hotpotqa/hotpot.git && git fetch --depth 1 origin 3635853403a8735609ee997664e1528f4480762a && git checkout --detach FETCH_HEAD
COPY grade.py /grader/grade.py
ENTRYPOINT []
