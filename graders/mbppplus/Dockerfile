FROM python:3.11-slim
RUN pip install --no-cache-dir evalplus==0.2.0
RUN python -c "from evalplus.data import get_mbpp_plus_hash; assert get_mbpp_plus_hash() == 'ebe95df0d5220f00cedd6f4bddea35e4', 'Unexpected MBPP+ dataset version'"
RUN python -c "from evalplus.data import get_mbpp_plus, get_mbpp_plus_hash; from evalplus.evaluate import get_groundtruth; from evalplus.eval._special_oracle import MBPP_OUTPUT_NOT_NONE_TASKS; p=get_mbpp_plus(); get_groundtruth(p,get_mbpp_plus_hash(),MBPP_OUTPUT_NOT_NONE_TASKS)"
COPY grade.py /grader/grade.py
ENTRYPOINT []
