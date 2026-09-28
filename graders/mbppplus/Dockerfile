FROM python:3.11-slim
RUN pip install --no-cache-dir evalplus==0.2.0
RUN python -c "from evalplus.data import get_mbpp_plus; get_mbpp_plus()"
COPY grade.py /grader/grade.py
ENTRYPOINT []
