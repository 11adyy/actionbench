"""Deep Agents author and use paired skills; LangGraph scripts execute in Docker."""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend
from deepagents.profiles import GeneralPurposeSubagentProfile, HarnessProfile, register_harness_profile
from langchain_core.tools import tool
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.errors import GraphRecursionError

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
    emits_jsonl=any(isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and node.func.attr=="dumps" and
        node.args and isinstance(node.args[0],ast.Dict) and any(isinstance(key,ast.Constant) and key.value=="kind" and
        isinstance(value,ast.Constant) and value.value=="result" for key,value in zip(node.args[0].keys,node.args[0].values))
        for node in ast.walk(tree))
    if "ctx.emit(" not in code and not emits_jsonl:
        raise ValueError("Skill script must emit a terminal result")
    blocked={"deepagents","subprocess","socket","requests","urllib","http","ctypes","openai"}
    if any(x.split(".")[0] in blocked for x in imports):raise ValueError("Skill script imports an unrestricted runtime")
    if any(isinstance(node,ast.While) for node in ast.walk(tree)):
        raise ValueError("Skill graph must be finite; while loops are forbidden")
    if "create_agent(" in code or "create_deep_agent(" in code:
        raise ValueError("A skill graph cannot contain an autonomous agent")
    for node in ast.walk(tree):
        if isinstance(node,ast.Call) and isinstance(node.func,ast.Name):
            if node.func.id in {"eval","exec","compile"}:
                raise ValueError("Dynamic execution is forbidden")
            if node.func.id=="__import__" and not (len(node.args)==1 and isinstance(node.args[0],ast.Constant) and node.args[0].value in {"sys","json","re","os","pathlib"}):
                raise ValueError("Dynamic imports are forbidden")
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


def _probe_package(config: dict, ledger: Ledger, manifest: dict, family: str, replica: int,
                   kind: str, destination: Path, root: Path, episode: str, revision: int):
    """Exercise the candidate in the actual sandbox on held-out development input."""
    data_root=Path(config["dataset_root"])
    example=next(item for item in manifest["tasks"] if item["family"]==family and item["split"]=="development")
    task_path=data_root/example["task"]
    task=json.loads(task_path.read_text())
    workspace=root/"development-probes"/family/str(replica)/kind/str(revision)
    workspace.mkdir(parents=True,exist_ok=False)
    shutil.copytree(task_path.parent/"files",workspace/"files")
    arguments={"query":task["prompt"],"task":task["prompt"],"files_dir":"/workspace/files"}
    before=ledger.db.execute("SELECT COUNT(*) FROM calls WHERE episode_id=? AND step LIKE ? AND state='completed'",
                             (episode,f"probe-{revision}:%")).fetchone()[0]
    provider=dict(config["provider"])
    probe_meter=Meter(ledger,float(config["campaign_limit_usd"]),float(provider["input_usd_per_million"]),
                      float(provider["cached_input_usd_per_million"]),float(provider["output_usd_per_million"]),1024)
    broker=_BrokerAdapter(_model(config,probe_meter),episode,float(config["creation_budget_usd"]))
    container=ContainerRunner(SimpleNamespace(execution=SimpleNamespace(**config["execution"])),broker)
    output=container.execute(episode,f"probe-{revision}",workspace,["python","/action/main.py"],arguments,
                             action_dir=destination,allow_llm=kind=="script_llm")
    if not isinstance(output,dict) or "kind" in output or "output" in output:
        raise ValueError("Graph emitted a nested protocol envelope; ctx.emit must receive only the answer object")
    required={"owner","decision","evidence_paths"} if family=="file_exploration" else {"summary","fact_ids","evidence_paths"}
    missing=required-output.keys()
    if missing:
        raise ValueError(f"Graph output is missing required fields: {sorted(missing)}")
    if kind=="script_llm":
        after=ledger.db.execute("SELECT COUNT(*) FROM calls WHERE episode_id=? AND step LIKE ? AND state='completed' AND provider_id IS NOT NULL",
                                (episode,f"probe-{revision}:%")).fetchone()[0]
        if after<=before:raise ValueError("LLM graph did not complete a real brokered model call")
    reference=json.loads((data_root/example["reference"]).read_text())
    validation=grade(family,json.dumps(output),reference,task_path.parent/"files")
    if validation["primary"]<=0:
        raise ValueError(f"Graph ran but failed the development-task quality check: {validation}. Inspect the files' contents, not only their names; parse the project or case identifier without trailing punctuation.")
    return output


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
            episode=f"creation:{family}:{replica}:{kind}"
            ledger.begin_episode(episode,episode,family,replica,kind,float(config["creation_budget_usd"]))
            error="Paired deterministic skill was unavailable"
            ledger.save_package(family,replica,kind,"failed",None,None,error)
            ledger.set_episode(episode,"failed",error=error)
            return None
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
        all_files=list(sorted(files.rglob("*.txt")))
        target=re.search(r"\b(?:project-\d+|[A-Z]\d{3,})\b",task["prompt"])
        relevant=[p for p in all_files if target and target.group().casefold() in p.read_text(errors="replace").casefold()]
        selected=list(dict.fromkeys(all_files[:2]+relevant[:1]))[:3]
        snippets={p.relative_to(files).as_posix():p.read_text(errors="replace")[:1200] for p in selected}
        examples.append({"prompt":task["prompt"],"sample_files":snippets})
        if len(examples)==3:break
    episode=f"creation:{family}:{replica}:{kind}"
    ledger.begin_episode(episode,episode,family,replica,kind,float(config["creation_budget_usd"]))
    if ledger.unresolved(episode):raise ValueError("Unknown provider outcome in skill creation")
    ledger.set_episode(episode,"running")
    body=("Use Python 3.11, langgraph.graph.StateGraph and a LangChain RunnableLambda. "
          "Build a small finite graph with named nodes; do not create an agent, planner, recursion, or while loop. "
          "Read exactly one JSON line from stdin; args=message['input'] is a DICT, and query=args['query'] is a STRING. "
          "Use ctx=ActionContext(args), graph.compile().invoke({'query':query}), and ctx.emit(result['output']). "
          "ctx.emit adds the result protocol envelope itself; NEVER pass {'kind':'result','output':...} to ctx.emit. "
          "Emit one result and exit. The direct Docker probe must run successfully before this package is accepted. "
          "Only access files under /workspace/files. "
          "Use from action_sdk import ActionContext and ctx=ActionContext(message['input']) to emit. ")
    if kind=="script_llm":
        body+=("For a semantic subtask, call ctx.call_llm(prompt, instructions='', max_output_tokens=512) "
               "inside one explicit graph node. This capability uses the same provider key and budget as the outer Deep Agent. ")
    else:body+="Do not call a model or import ChatOpenAI. "
    body+=("The output object must have owner, decision, evidence_paths. " if family=="file_exploration" else
           "The output object must have summary, fact_ids, evidence_paths. ")
    slug=family.replace("_","-")
    files_instruction=(f"Use write_file to create /main.py first, then /SKILL.md with YAML name: {slug} and description. "
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
                try:
                    agent.invoke({"messages":[{"role":"user","content":prompt}]},config={"configurable":{"thread_id":episode},"recursion_limit":24})
                except GraphRecursionError:
                    # Some creators keep commenting after writing the files.
                    # The frozen files still receive full structural checks.
                    pass
                if not (destination/"SKILL.md").is_file() or not (destination/"main.py").is_file():
                    try:
                        agent.invoke({"messages":[{"role":"user","content":"Finish writing the missing requested files now, then stop."}]},
                                     config={"configurable":{"thread_id":episode},"recursion_limit":12})
                    except GraphRecursionError:
                        pass
                last_error=None
                for revision in range(2):
                    try:
                        if not (destination/"SKILL.md").is_file() or not (destination/"main.py").is_file():
                            raise ValueError("Creator did not write SKILL.md and main.py")
                        if paired_skill is not None and (destination/"SKILL.md").read_bytes()!=paired_skill:
                            raise ValueError("Creator changed the paired skill instructions")
                        validate_script(destination/"main.py",kind)
                        (destination/"action_sdk.py").write_text(Path(action_sdk.__file__).read_text())
                        if not re.search(r"(?m)^name:\s*[a-z0-9]+(?:-[a-z0-9]+)*\s*$",(destination/"SKILL.md").read_text()):
                            raise ValueError("Skill YAML name must be lowercase words with single hyphens, e.g. file-exploration")
                        _probe_package(config,ledger,manifest,family,replica,kind,destination,root,episode,revision)
                        last_error=None
                        break
                    except Exception as exc:
                        if ledger.unresolved(episode):raise
                        last_error=exc
                        if revision==1:break
                        feedback=("The candidate failed its real Docker development probe or validation. "
                                  "Fix /main.py, then stop. Keep /SKILL.md unchanged if it exists. "
                                  "Read message['input'] as a dict; call ctx.emit(answer_object), not ctx.emit({'kind':'result','output':...}). "
                                  f"Failure: {str(exc)[-1400:]}")
                        try:
                            with model_scope(episode,"creator-repair",float(config["creation_budget_usd"])):
                                agent.invoke({"messages":[{"role":"user","content":feedback}]},
                                             config={"configurable":{"thread_id":episode},"recursion_limit":16})
                        except GraphRecursionError:
                            pass
                if last_error is not None:raise last_error
        hashed=tree_hash(destination)
        (destination/"package.json").write_text(json.dumps({"family":family,"replica":replica,"kind":kind,"sha256":hashed},indent=2))
        ledger.save_package(family,replica,kind,"completed",str(destination),hashed)
        ledger.set_episode(episode,"completed",answer=str(destination))
        return destination
    except Exception as exc:
        status="blocked" if ledger.unresolved(episode) else "failed"
        ledger.save_package(family,replica,kind,status,str(destination),None,str(exc)[-2000:])
        ledger.set_episode(episode,status,error=str(exc)[-2000:])
        if status=="blocked":raise
        return None


class _BrokerAdapter:
    def __init__(self,model,episode,limit):self.model,self.episode,self.limit=model,episode,limit
    def call(self,episode_id,step,instructions,prompt,max_output_tokens,timeout_seconds=None):
        if episode_id!=self.episode:raise ValueError("Wrong episode for graph model call")
        with model_scope(episode_id,step,self.limit):
            result=self.model.invoke([("system",instructions or "Follow the task"),("human",prompt)])
        content=result.content
        if isinstance(content,list):
            content="\n".join(part.get("text","") for part in content if isinstance(part,dict) and part.get("type") in ("text","output_text"))
        if not isinstance(content,str) or not content:
            raise ValueError("Model response contains no text for the skill graph")
        return SimpleNamespace(text=content)


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
    invocation_lock=threading.RLock()
    adapter=_BrokerAdapter(model,episode,budget)
    docker_config=SimpleNamespace(execution=SimpleNamespace(**config["execution"]))
    container=ContainerRunner(docker_config,adapter)

    @tool
    def run_skill(query: str) -> str:
        """Run the frozen skill's finite LangGraph on /workspace/files for this query."""
        nonlocal ordinal
        with invocation_lock:
            if not isinstance(query,str) or not query.strip():raise ValueError("Skill query must be nonempty text")
            args={"query":query,"task":query,"files_dir":"/workspace/files"}
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
            "You may call run_skill with the task query as plain text. "
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
        ledger.set_episode(episode,status,score=0.0 if status=="failed" else None,error=str(exc)[-2000:],duration=time.monotonic()-start)
