from pathlib import Path
from app.release.models import FeedbackAnnotation
from app.release.service import ReleaseService
from app.storage.db import migrate,open_database

def test_feedback_quarantined_not_dataset_mutation(tmp_path):
    root=Path('evals/foundation/1.0.0')
    before={p.name:p.read_bytes() for p in root.iterdir()}
    note=tmp_path/'annotation.json'; note.write_text('{"label":"review"}')
    conn=open_database(tmp_path/'registry.db'); migrate(conn)
    annotation=FeedbackAnnotation(annotation_ref=str(note),annotation_hash=__import__('hashlib').sha256(note.read_bytes()).hexdigest(),
        agent_name='foundation',dataset_hash='a'*64)
    queued=ReleaseService(conn).queue_feedback(annotation)
    assert queued.status=='QUARANTINED'
    assert queued.annotation.privacy_review=='PENDING'
    assert queued.annotation.rights_review=='PENDING'
    assert queued.annotation.adjudication_review=='PENDING'
    assert {p.name:p.read_bytes() for p in root.iterdir()}==before
    conn.close()
