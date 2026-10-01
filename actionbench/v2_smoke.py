"""Real Docker and provider gate; deliberately no mocked success path."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from . import action_sdk
from .runner import ContainerRunner
from .v2_model import model_scope
from .v2_runner import _BrokerAdapter, _model
from .v2_store import Ledger


GRAPH = '''import json,sys
from langgraph.graph import StateGraph, START, END
from langchain_core.runnables import RunnableLambda
from action_sdk import ActionContext
message=json.loads(sys.stdin.readline())
ctx=ActionContext(message['input'])
def answer(state):
    text=ctx.call_llm('Reply with exactly OK.',instructions='Return exactly OK.',max_output_tokens=16)
    return RunnableLambda(lambda value:{'answer':value}).invoke(text)
graph=StateGraph(dict)
graph.add_node('answer',answer)
graph.add_edge(START,'answer')
graph.add_edge('answer',END)
ctx.emit(graph.compile().invoke({}))
'''


def smoke(cfg:dict,ledger:Ledger,meter,root:Path)->dict:
    episode="integration:deepagent-v2-graph"
    prior=ledger.begin_episode(episode,episode,"integration",0,"script_llm",0.03)
    if prior["status"]=="completed":
        return {"passed":True,"resumed":True,"provider_id":ledger.db.execute("SELECT provider_id FROM calls WHERE episode_id=? AND state='completed' LIMIT 1",(episode,)).fetchone()[0]}
    if prior["status"]!="queued" or ledger.unresolved(episode):
        raise ValueError("Smoke has unresolved earlier provider outcome")
    package=root/"smoke"/"skill"
    package.mkdir(parents=True,exist_ok=True)
    (package/"main.py").write_text(GRAPH)
    (package/"action_sdk.py").write_text(Path(action_sdk.__file__).read_text())
    workspace=root/"smoke"/"workspace"
    model=_model(cfg,meter)
    adapter=_BrokerAdapter(model,episode,0.03)
    container=ContainerRunner(SimpleNamespace(execution=SimpleNamespace(**cfg["execution"])),adapter)
    ledger.set_episode(episode,"running")
    output=container.execute(episode,"probe",workspace,["python","/action/main.py"],{},action_dir=package,allow_llm=True)
    row=ledger.db.execute("SELECT state,provider_id FROM calls WHERE episode_id=? ORDER BY rowid LIMIT 1",(episode,)).fetchone()
    if output.get("answer","").strip()!="OK" or not row or row["state"]!="completed" or not row["provider_id"]:
        raise ValueError("Real graph/model smoke did not return an auditable response")
    ledger.set_episode(episode,"completed",answer=json.dumps(output))
    return {"passed":True,"provider_id":row["provider_id"],"graph_output":output}
