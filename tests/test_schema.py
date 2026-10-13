"""The code must match the API schema of the TDLib build that is installed.

This is the check that matters when a new TDLib comes out: it fails when a
method or field this code sends has been renamed or removed.
"""

import inspect
import os

import pytest

from tgmcp import api, fmt, manage, schema
from tgmcp.tdlib import TgError, execute

SAMPLE = """
double ? = Double;
string ? = String;
vector {t:Type} # [ t ] = Vector t;

//@description A file on disk @path Local path
inputFileLocal path:string = InputFile;
//@description A file by id @id Identifier
inputFileId id:int32 = InputFile;

//@class InputFile @description Points to a file

//@description A document @document The file @caption Caption
inputDocument document:InputFile caption:string = InputDocument;

---functions---

//@description Sends a document to a chat
//@chat_id Target chat
//@doc The document
//@tags Free tags
sendDocument chat_id:int53 doc:inputDocument tags:vector<string> = Ok;
"""


@pytest.fixture(scope="module")
def sample():
    return schema.Schema(SAMPLE)


def test_parse(sample):
    assert set(sample.defs) == {"inputFileLocal", "inputFileId", "inputDocument", "sendDocument"}
    assert sample.classes["InputFile"] == ["inputFileLocal", "inputFileId"]
    send = sample.describe("sendDocument")
    assert send["kind"] == "method" and send["params"][0] == {
        "name": "chat_id", "type": "int53", "description": "Target chat"}
    assert sample.describe("InputFile")["description"] == "Points to a file"
    assert [r["name"] for r in sample.search("document chat")] == ["sendDocument"]


@pytest.mark.parametrize("params, message", [
    ({"chat_id": 1, "doc": {"document": {"@type": "inputFileLocal", "path": "x"}}}, None),
    ({"chatid": 1}, "did you mean chat_id"),
    ({"doc": {"document": {"path": "x"}}}, 'needs "@type"'),
    ({"doc": {"document": {"@type": "inputDocument"}}}, "is not a InputFile"),
    ({"doc": {"document": {"@type": "inputFileLocal", "name": "x"}}}, "no field 'name'"),
])
def test_validate(sample, params, message):
    if message is None:
        sample.validate_call("sendDocument", params)
        return
    with pytest.raises(TgError, match=message):
        sample.validate_call("sendDocument", params)


def test_unknown_method_suggests(sample):
    with pytest.raises(TgError, match="did you mean sendDocument"):
        sample.validate_call("sendDocumet", {})


def test_lint(sample):
    good = 'x._req("sendDocument", chat_id=1, timeout=5, doc={"@type": "inputDocument"})'
    assert sample.lint(good) == []
    bad = 'x._req("sendDocument", chat=1)\ny = {"@type": "inputFileLocal", "file": 1}'
    assert sample.lint(bad) == ["line 1: sendDocument has no field 'chat'",
                                "line 2: inputFileLocal has no field 'file'"]


def test_sources_match_installed_tdlib(tmp_path):
    commit = execute("getOption", name="commit_hash")["value"]
    live = schema.load(commit, tmp_path)
    if live is None:
        if os.environ.get("TG_REQUIRE_SCHEMA"):
            pytest.fail(f"could not download the TDLib schema for commit {commit}")
        pytest.skip("TDLib schema is not reachable")
    problems = [f"{m.__name__}: {p}" for m in (api, manage, fmt)
                for p in live.lint(inspect.getsource(m))]
    assert problems == []
