import json
from pathlib import Path
from app.contracts.export import export_schemas

def test_state_contracts_have_versioned_snapshots(tmp_path):
    exported=export_schemas(tmp_path)
    assert "state.json" in exported
    schema=json.loads((tmp_path/"state.json").read_text())
    assert schema["schema_version"] == "1.0"
    assert set(schema["models"]) == {"StateEvent","StorySnapshot","TransitionRequest"}
    assert schema == json.loads(Path("schemas/v1/state.json").read_text())
