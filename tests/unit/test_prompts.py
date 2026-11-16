import hashlib,json
from pathlib import Path
import pytest
from app.prompts import load_prompt

def make_manifest(tmp_path,path="../foundation/test.v1.md",digest=None):
    base=tmp_path/"prompts"
    (base/"manifests").mkdir(parents=True)
    (base/"foundation").mkdir()
    content=b"Synthetic fixture only."
    (base/"foundation/test.v1.md").write_bytes(content)
    manifest=base/"manifests/test.v1.json"
    manifest.write_text(json.dumps(dict(prompt_id="foundation",prompt_version="foundation.v1",path=path,sha256=digest or hashlib.sha256(content).hexdigest(),schema_version="1.0")))
    return manifest

def test_prompt_hash_mismatch(tmp_path):
    with pytest.raises(ValueError,match="hash"):
        load_prompt(make_manifest(tmp_path,digest="0"*64))

def test_prompt_cannot_escape_prompt_root(tmp_path):
    outside=tmp_path/"private.md"
    outside.write_text("not a prompt")
    with pytest.raises(ValueError,match="root"):
        load_prompt(make_manifest(tmp_path,path="../../private.md"))

def test_pinned_prompt_loads_and_detects_edits(tmp_path):
    manifest=make_manifest(tmp_path)
    spec=load_prompt(manifest)
    assert spec.prompt_version == "foundation.v1"
    (manifest.parent/"../foundation/test.v1.md").write_text("Changed")
    with pytest.raises(ValueError,match="hash"):
        load_prompt(manifest)

def test_repository_synthetic_prompt_is_pinned():
    spec=load_prompt(Path("prompts/manifests/foundation.v1.json"))
    assert spec.prompt_id == "foundation"
