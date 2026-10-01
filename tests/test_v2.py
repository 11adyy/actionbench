"""Protocol regressions; hosted Docker/API smoke remains the real integration gate."""
import json
import tempfile
import unittest
from pathlib import Path

from actionbench.v2_data import grade, prepare_custom
from actionbench.v2_store import Ledger
from actionbench.v2_runner import _BrokerAdapter, validate_script
from types import SimpleNamespace


class V2Tests(unittest.TestCase):
    def test_controlled_graph_accepts_valid_jsonl_without_sdk_emit(self):
        script='''import json
from langgraph.graph import StateGraph, START, END
from langchain_core.runnables import RunnableLambda
from action_sdk import ActionContext
ctx=ActionContext({})
graph=StateGraph(dict)
graph.add_node("answer",RunnableLambda(lambda x:x))
graph.add_edge(START,"answer")
graph.add_edge("answer",END)
result=graph.compile().invoke({})
print(json.dumps({"kind":"result","output":result}))
'''
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"main.py";path.write_text(script)
            validate_script(path,"script_only")
            path.write_text(script.replace('print(json.dumps({"kind":"result","output":result}))',
                                           'print(json.dumps({"result":{"kind":"result","output":result}}))'))
            with self.assertRaises(ValueError):validate_script(path,"script_only")

    def test_responses_content_blocks_reach_langgraph_as_text(self):
        class Model:
            def invoke(self,messages):
                return SimpleNamespace(content=[{"type":"text","text":"OK","phase":"final_answer"}])
        adapter=_BrokerAdapter(Model(),"e",0.01)
        self.assertEqual(adapter.call("e","step","","prompt",16).text,"OK")

    def test_dataset_is_disjoint_and_private_answers_are_not_in_public_task(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/"data"
            manifest=prepare_custom(root,development=2,test=3)
            self.assertEqual(len(manifest["tasks"]),10)
            development={x["id"] for x in manifest["tasks"] if x["split"]=="development"}
            test={x["id"] for x in manifest["tasks"] if x["split"]=="test"}
            self.assertFalse(development & test)
            example=next(x for x in manifest["tasks"] if x["family"]=="file_exploration" and x["split"]=="test")
            public=json.loads((root/example["task"]).read_text())
            private=json.loads((root/example["reference"]).read_text())
            self.assertNotIn(private["decision"],json.dumps(public))
            files=root/example["task"].replace("task.json","files")
            correct=json.dumps(private)
            self.assertEqual(grade("file_exploration",correct,private,files)["primary"],1)
            self.assertLess(grade("file_exploration",json.dumps({**private,"evidence_paths":["wrong.txt"]}),private,files)["primary"],1)

    def test_ledger_blocks_unknown_response_and_enforces_both_limits(self):
        with tempfile.TemporaryDirectory() as temp:
            db=Path(temp)/"study.sqlite3"
            ledger=Ledger(db,"study",{"model":"x"})
            ledger.begin_episode("a","task","files",0,"script_llm",0.01)
            ledger.reserve("request-1","a","graph",0.004,0.01,0.02)
            self.assertTrue(ledger.unresolved("a"))
            with self.assertRaises(ValueError):ledger.reserve("request-1","a","graph",0.004,0.01,0.02)
            ledger.complete("request-1",input_tokens=20,cached_tokens=0,output_tokens=5,actual_usd=0.003,provider_id="resp-real")
            self.assertFalse(ledger.unresolved("a"))
            with self.assertRaises(ValueError):ledger.reserve("request-2","a","graph",0.008,0.01,0.02)
            ledger.close()
            with self.assertRaises(ValueError):Ledger(db,"study",{"model":"different"})

    def test_frozen_invocation_never_silently_replays(self):
        with tempfile.TemporaryDirectory() as temp:
            ledger=Ledger(Path(temp)/"db.sqlite3","study",{})
            self.assertIsNone(ledger.start_invocation("e",0,"input-hash"))
            self.assertEqual(ledger.invocation("e",0)["state"],"submitted")
            with self.assertRaises(ValueError):ledger.start_invocation("e",0,"changed-input")
            ledger.finish_invocation("e",0,{"answer":1})
            self.assertEqual(ledger.start_invocation("e",0,"input-hash")["state"],"completed")


if __name__=="__main__":unittest.main()
