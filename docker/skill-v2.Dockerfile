FROM python:3.11-slim
RUN pip install --no-cache-dir langchain-core==1.6.6 langgraph==1.2.12
WORKDIR /workspace
