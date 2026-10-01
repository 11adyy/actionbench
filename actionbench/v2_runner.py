"""Deep Agents author and use paired skills; LangGraph scripts execute in Docker."""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path
from types import SimpleNamespace

from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend
from deepagents.profiles import GeneralPurposeSubagentProfile, HarnessProfile, register_harness_profile
from langchain_core.tools import tool
from langgraph.checkpoint.sqlite import SqliteSaver

from . import action_sdk
from .runner import ContainerRunner
from .v2_model import Meter, make_model, model_scope
from .v2_store import Ledger, digest
from .v2_data import grade


KINDS = ("script_only", "script_llm")


def tree_hash(path: Path) -> str:
    h=hashlib.sha256()
    for file in sorted(path.rglob("*")):
        if file.is_file() and file.name != "package.json":
            h.update(file.relative_to(path).as_posix().encode());h.update(file.read_bytes())
    return h.hexdigest()


def validate_script(script: Path, kind: str):
    code=script.read_text()
    tree=ast.parse(code)
    imports={(alias.name if isinstance(node,ast.Import) else node.module or "") for node in ast.walk(tree)
             if isinstance(node,(ast.Import,ast.ImportFrom)) for alias in (node.names if isinstance(node,ast.Import) else [None])}
    if not any(x.startswith("langgraph") for x in imports):raise ValueError("Skill script must use LangGraph")
    if not any(x.startswith("langchain") for x in imports):raise ValueError("Skill script must use LangChain")
    if "StateGraph(" not in code or ".compile(" not in code or ".invoke(" not in code:
        raise ValueError("Skill script needs an invoked, compiled StateGraph")
    if "ctx.emit(" not in code:raise ValueError("Skill script must emit a terminal result")
    blocked={"deepagents","subprocess","socket","requests","urllib","http","ctypes","openai"}
    if any(x.split(".")[0] in blocked for x in imports):raise ValueError("Skill script imports an unrestricted runtime")
    if any(isinstance(node,ast.While) for node in ast.walk(tree)):
        raise ValueError("Skill graph must be finite; while loops are forbidden")
    if "create_agent(" in code or "create_deep_agent(" in code:
        raise ValueError("A skill graph cannot contain an autonomous agent")
    if any(isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id in {"eval","exec","compile","__import__"} for node in ast.walk(tree)):
        raise ValueError("Dynamic execution is forbidden")
    if kind=="script_only" and any(word in code for word in ("call_llm", "ChatOpenAI", "OPENAI_API_KEY")):
        raise ValueError("Deterministic skill cannot call a model")
    if kind=="script_llm" and "call_llm(" not in code:
        raise ValueError("LLM skill must use the metered model capability")


def _message_text(result) -> str:
    for message in reversed(result.get("messages", [])):
        if getattr(message,"type",None)=="ai" and getattr(message,"content",None):
            content=message.content
            if isinstance(content,str):return content
            return "\n".join(item.get("text","") for item in content if isinstance(item,dict))
    raise ValueError("Deep Agent produced no answer")


def _backend(root: Path):
    return FilesystemBackend(root_dir=root,virtual_mode=True)


def _model(config: dict, meter: Meter):
    provider=dict(config["provider"])
    provider["api_key_env_value"]=os.environ.get(provider.get("api_key_env","OPENAI_API_KEY"))
    if not provider["api_key_env_value"]:raise ValueError("Provider API key is missing from the runtime environment")
    return make_model(provider,meter)


def _agent(model, provider_model: str, **kwargs):
    # Deep Agents otherwise adds its general-purpose subagent by default.
    register_harness_profile("openai:"+provider_model,
                             HarnessProfile(general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False)))
    return create_deep_agent(model=model,subagents=[],**kwargs)


def create_skill(config: dict, ledger: Ledger, meter: Meter, manifest: dict, family: str, replica: int, kind: str, root: Path):
    if kind not in KINDS:raise ValueError(kind)
    destination=root/"packages"/family/str(replica)/kind
    prior=ledger.package(family,replica,kind)
    if prior:
        if prior["status"]=="completed" and destination.exists() and tree_hash(destination)==prior["sha256"]:return destination
        if prior["status"]=="failed":return None
        raise ValueError("Incomplete package requires inspection before resume")
    paired_skill=None
    if kind=="script_llm":
        base=ledger.package(family,replica,"script_only")
        if not base or base["status"]!="completed":
            raise ValueError("Paired deterministic skill must be created first")
        source=Path(base["path"])
        if tree_hash(source)!=base["sha256"]:raise ValueError("Paired skill hash changed")
        paired_skill=(source/"SKILL.md").read_bytes()
    destination.mkdir(parents=True,exist_ok=False)
    if paired_skill is not None:
        (destination/"SKILL.md").write_bytes(paired_skill)
    examples=[]
    data_root=Path(config["dataset_root"])
    allowed={family,"qmsum"} if family=="file_summary" else {family}
    for item in manifest["tasks"]:
        if item["family"] not in allowed or item["split"]!="development":continue
        task=json.loads((data_root/item["task"]).read_text())
        files=data_root/item["task"].replace("task.json","files")
        snippets={p.relative_to(files).as_posix():p.read_text()[:1200] for p in list(sorted(files.rglob("*.txt")))[:3]}
        examples.append({"prompt":task["prompt"],"sample_files":snippets})
        if len(examples)==3:break
    episode=f"creation:{family}:{replica}:{kind}"
    ledger.begin_episode(episode,episode,family,replica,kind,float(config["creation_budget_usd"]))
    if ledger.unresolved(episode):raise ValueError("Unknown provider outcome in skill creation")
    ledger.set_episode(episode,"running")
    body=("Use Python 3.11, langgraph.graph.StateGraph and a LangChain RunnableLambda. "
          "Build a small finite graph with named nodes; do not create an agent, planner, recursion, or while loop. "
          "Read exactly one JSON line from stdin; input is message['input']. "
          "Emit exactly one JSON line {'kind':'result','output':{...}} then exit. "
          "Only access files under /workspace/files. "
          "Use from action_sdk import ActionContext and ctx=ActionContext(message['input']) to emit. ")
    if kind=="script_llm":
        body+=("For a semantic subtask, call ctx.call_llm(prompt, instructions='', max_output_tokens=512) "
               "inside one explicit graph node. This capability uses the same provider key and budget as the outer Deep Agent. ")
    else:body+="Do not call a model or import ChatOpenAI. "
    files_instruction=("Use write_file to create /SKILL.md with YAML name and description, and /main.py. "
                       if kind=="script_only" else
                       "The paired /SKILL.md already exists. Read it and create only /main.py; do not edit /SKILL.md. ")
    prompt=(f"Create one reusable {kind} skill for family {family}. " + files_instruction +
            "The skill should tell the user agent when to execute main.py through run_skill. "
            "Create files now; do not just describe them. " + body +
            "Samples from development only: " + json.dumps(examples,ensure_ascii=False)[:12000])
    try:
        checkpoint=root/"checkpoints"/(digest(episode)+".sqlite3")
        checkpoint.parent.mkdir(parents=True,exist_ok=True)
        with SqliteSaver.from_conn_string(str(checkpoint)) as saver:
            agent=_agent(_model(config,meter),config["provider"]["model"],backend=_backend(destination),checkpointer=saver,
                         system_prompt="You write reusable, compact skills. Use filesystem write_file tools to create the requested files. Do not use subagents.")
            with model_scope(episode,"creator",float(config["creation_budget_usd"])):
                agent.invoke({"messages":[{"role":"user","content":prompt}]},config={"configurable":{"thread_id":episode},"recursion_limit":16})
        if not (destination/"SKILL.md").is_file() or not (destination/"main.py").is_file():
            raise ValueError("Creator did not write SKILL.md and main.py")
        if paired_skill is not None and (destination/"SKILL.md").read_bytes()!=paired_skill:
            raise ValueError("Creator changed the paired skill instructions")
        validate_script(destination/"main.py",kind)
        (destination/"action_sdk.py").write_text(Path(action_sdk.__file__).read_text())
        if not re.search(r"(?m)^name:\s*",(destination/"SKILL.md").read_text()):
            raise ValueError("Skill YAML frontmatter must name the skill")
        hashed=tree_hash(destination)
        (destination/"package.json").write_text(json.dumps({"family":family,"replica":replica,"kind":kind,"sha256":hashed},indent=2))
        ledger.save_package(family,replica,kind,"completed",str(destination),hashed)
        ledger.set_episode(episode,"completed",answer=str(destination))
        return destination
    except Exception as exc:
        status="blocked" if ledger.unresolved(episode) else "failed"
        ledger.save_package(family,replica,kind,status,str(destination),None,str(exc)[:500])
        ledger.set_episode(episode,status,error=str(exc)[:500])
        if status=="blocked":raise
        return None


class _BrokerAdapter:
    def __init__(self,model,episode,limit):self.model,self.episode,self.limit=model,episode,limit
    def call(self,episode_id,step,instructions,prompt,max_output_tokens,timeout_seconds=None):
        if episode_id!=self.episode:raise ValueError("Wrong episode for graph model call")
        with model_scope(episode_id,step,self.limit):
            result=self.model.invoke([("system",instructions or "Follow the task"),("human",prompt)])
        return SimpleNamespace(text=result.content if isinstance(result.content,str) else str(result.content))


def run_episode(config:dict,ledger:Ledger,meter:Meter,task:dict,replica:int,kind:str,budget:float,root:Path):
    if kind not in KINDS:raise ValueError(kind)
    family=task["family"]
    skill_family="file_summary" if family=="qmsum" else family
    episode=f"test:{task['id']}:{replica}:{kind}:{budget:.3f}"
    existing=ledger.begin_episode(episode,task["id"],family,replica,kind,budget)
    if existing["status"] in ("completed","failed"):return
    if existing["status"] in ("running","blocked"):
        ledger.set_episode(episode,"blocked",error="Interrupted episode needs provider and checkpoint audit")
        return
    if ledger.unresolved(episode):
        ledger.set_episode(episode,"blocked",error="Provider outcome is unknown")
        return
    package=ledger.package(skill_family,replica,kind)
    if not package or package["status"]!="completed":
        ledger.set_episode(episode,"failed",score=0.0,grader={"reason":"package_unavailable"},error="Skill package unavailable")
        return
    package_dir=Path(package["path"])
    if tree_hash(package_dir)!=package["sha256"]:raise ValueError("Frozen package hash changed")
    data_root=Path(config["dataset_root"])
    task_path=data_root/task["task"]
    task_data=json.loads(task_path.read_text())
    workspace=root/"workspaces"/digest(episode)
    if not workspace.exists():
        workspace.mkdir(parents=True)
        shutil.copytree(task_path.parent/"files",workspace/"files")
        shutil.copytree(package_dir,workspace/"skills"/"task-skill")
    backend=_backend(workspace)
    model=_model(config,meter)
    ordinal=0
    adapter=_BrokerAdapter(model,episode,budget)
    docker_config=SimpleNamespace(execution=SimpleNamespace(**config["execution"]))
    container=ContainerRunner(docker_config,adapter)

    @tool
    def run_skill(input_json: str) -> str:
        """Run the frozen skill's finite LangGraph over the task files. Input is a JSON object string."""
        nonlocal ordinal
        try: args=json.loads(input_json)
        except ValueError as exc:raise ValueError("Skill input must be JSON") from exc
        if not isinstance(args,dict):raise ValueError("Skill input must be an object")
        current=ordinal;ordinal+=1
        input_hash=digest(args)
        saved=ledger.start_invocation(episode,current,input_hash)
        if saved:
            if saved["state"]=="completed":return saved["output_json"]
            raise ValueError("Skill invocation outcome unknown; inspect before resume")
        output=container.execute(episode,f"graph-{current}",workspace,["python","/action/main.py"],args,
                                 action_dir=package_dir,allow_llm=kind=="script_llm")
        ledger.finish_invocation(episode,current,output)
        return json.dumps(output,ensure_ascii=False)

    prompt=(task_data["prompt"]+"\nRead the relevant skill instructions under /skills/task-skill/SKILL.md. "
            "You may call run_skill with a JSON object containing the task and relevant paths. "
            "Answer only with the requested JSON. Files are under /files and /workspace/files in the execution tool.")
    start=time.monotonic()
    ledger.set_episode(episode,"running")
    try:
        checkpoint=root/"checkpoints"/(digest(episode)+".sqlite3")
        checkpoint.parent.mkdir(parents=True,exist_ok=True)
        with SqliteSaver.from_conn_string(str(checkpoint)) as saver:
            agent=_agent(model,config["provider"]["model"],backend=backend,skills=["/skills/"],tools=[run_skill],checkpointer=saver,
                         system_prompt="Solve the task using the available files and skill. Follow the skill when useful. Do not use subagents.")
            with model_scope(episode,"outer-agent",budget):
                response=agent.invoke({"messages":[{"role":"user","content":prompt}]},
                                      config={"configurable":{"thread_id":episode},"recursion_limit":20})
        answer=_message_text(response)
        reference=json.loads((data_root/task["reference"]).read_text())
        score=grade(family,answer,reference,task_path.parent/"files")
        ledger.set_episode(episode,"completed",score=score["primary"],grader=score,answer=answer,duration=time.monotonic()-start)
    except Exception as exc:
        status="blocked" if ledger.unresolved(episode) or isinstance(exc,(OSError,KeyboardInterrupt)) else "failed"
        ledger.set_episode(episode,status,score=0.0 if status=="failed" else None,error=str(exc)[:500],duration=time.monotonic()-start)
