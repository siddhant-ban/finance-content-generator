"""Load immutable, hash-pinned local prompt manifests without executing text."""
import hashlib
from pathlib import Path
from typing import Literal
from app.contracts.common import Nonempty, Sha256, StrictModel

class PromptSpec(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    prompt_id: Nonempty
    prompt_version: Nonempty
    path: Nonempty
    sha256: Sha256

def load_prompt(manifest_path: Path) -> PromptSpec:
    manifest_path = manifest_path.resolve()
    spec = PromptSpec.model_validate_json(manifest_path.read_bytes())
    root = manifest_path.parent.parent.resolve()
    target = (manifest_path.parent / spec.path).resolve()
    if not target.is_relative_to(root):
        raise ValueError("prompt path escapes prompt root")
    if hashlib.sha256(target.read_bytes()).hexdigest() != spec.sha256:
        raise ValueError("prompt content hash mismatch")
    return spec
