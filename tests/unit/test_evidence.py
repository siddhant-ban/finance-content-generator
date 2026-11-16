from datetime import datetime,timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID
import json
import pytest
from pydantic import ValidationError
from app.contracts.evidence import SourceRecord,ClaimRevision,EvidenceRef,VerificationRecord,NumericObservation,ClaimUse,validate_evidence,validate_claim_uses
from app.contracts.workflow import ReviewManifest
from app.contracts.common import ArtifactRef

@pytest.fixture
def source():
    return SourceRecord(source_id=UUID(int=10),revision=1,original_url="https://example.invalid/release",canonical_url="https://example.invalid/release",publisher="Synthetic fixture",source_class="primary",published_at=None,publication_time_reason="not supplied",retrieved_at=datetime(2026,11,10,tzinfo=timezone.utc),locator="table 1",rights="metadata_only",usage_notes="Synthetic metadata fixture")

@pytest.fixture
def claim(source):
    ref=EvidenceRef(source_id=source.source_id,source_revision=1,source_record_hash=source.record_hash())
    return ClaimRevision(claim_id=UUID(int=20),revision=1,text="Synthetic observation",claim_type="fact",materiality="material",evidence_refs=(ref,))

@pytest.fixture
def verdict(claim):
    return VerificationRecord(claim_id=claim.claim_id,claim_revision=1,claim_hash=claim.record_hash(),status="VERIFIED",evidence_refs=claim.evidence_refs)

def test_unknown_publication_requires_reason(source):
    data=source.model_dump()
    data["publication_time_reason"]=None
    with pytest.raises(ValidationError):
        SourceRecord(**data)
    assert source.published_at is None
    assert source.publication_time_reason == "not supplied"

def test_unknown_rights_disallows_excerpt(source):
    data=source.model_dump()|{"rights":"unknown","excerpt":"copied body"}
    with pytest.raises(ValidationError):
        SourceRecord(**data)

@pytest.mark.parametrize("bad",["file:///etc/passwd","javascript:alert(1)","https://user:password@example.invalid"])
def test_unsafe_source_url_rejected(source,bad):
    with pytest.raises(ValidationError):
        SourceRecord(**(source.model_dump()|{"original_url":bad}))

def test_numeric_basis_vintage_units_explicit():
    with pytest.raises(ValidationError):
        NumericObservation(value=Decimal("0.5"),unit="percent")
    observation=NumericObservation(value=Decimal("0.5"),unit="percent",currency=None,period="synthetic period",geography="synthetic location",seasonal_adjustment="unknown",basis="unknown",revision_status="unknown",vintage=None,classification="actual",comparison_baseline=None)
    assert observation.unit == "percent"
    assert NumericObservation.model_validate_json(observation.model_dump_json()) == observation

def test_stale_source_cannot_verify_current_claim(source,claim,verdict):
    changed=SourceRecord(**(source.model_dump()|{"revision":2,"locator":"corrected table"}))
    assert validate_evidence([claim],[changed],[verdict]).ready is False

def test_missing_material_verdict_blocks(source,claim):
    report=validate_evidence([claim],[source],[])
    assert report.ready is False
    assert report.critical_failures

def test_current_verified_evidence_ready(source,claim,verdict):
    assert validate_evidence([claim],[source],[verdict]).ready is True

def test_orphan_reference_and_stale_claim_fail(source,claim,verdict):
    assert validate_evidence([claim],[],[verdict]).ready is False
    changed=ClaimRevision(**(claim.model_dump()|{"revision":2,"text":"Corrected synthetic observation"}))
    assert validate_evidence([changed],[source],[verdict]).ready is False

def test_each_use_preserves_caveat(source,claim):
    caveat="Synthetic estimate is provisional."
    nuanced=ClaimRevision(**(claim.model_dump()|{"caveats":(caveat,)}))
    verified=VerificationRecord(claim_id=nuanced.claim_id,claim_revision=1,claim_hash=nuanced.record_hash(),status="VERIFIED_WITH_CAVEAT",evidence_refs=nuanced.evidence_refs,caveat=caveat)
    good=ClaimUse(claim_id=claim.claim_id,claim_revision=1,text="Synthetic observation. "+caveat)
    bad=ClaimUse(claim_id=claim.claim_id,claim_revision=1,text="Synthetic observation.")
    assert validate_claim_uses([good],[nuanced],[verified]).ready is True
    assert validate_claim_uses([good,bad],[nuanced],[verified]).ready is False

def test_analysis_label_does_not_exempt_facts():
    interpretation=ClaimRevision(claim_id=UUID(int=30),revision=1,text="Analysis with supporting facts",claim_type="interpretation",materiality="material")
    assert validate_evidence([interpretation],[],[]).ready is False

def test_duplicate_ids_and_empty_dossier_block(source,claim,verdict):
    assert validate_evidence([claim,claim],[source],[verdict]).ready is False
    assert validate_evidence([],[],[]).ready is False

def test_review_manifest_complete_and_hash_bound():
    ref=ArtifactRef(sha256="a"*64,media_type="application/json",size_bytes=10)
    with pytest.raises(ValidationError):
        ReviewManifest(story_id=UUID(int=1),revision=1,kind="synthetic_foundation",script=ref)
    parts={name:ref for name in ("script","dossier","verification","render","subtitles","captions","charts","attributions","asset_register")}
    first=ReviewManifest(story_id=UUID(int=1),revision=1,kind="synthetic_foundation",**parts)
    changed=ReviewManifest(**(first.model_dump()|{"captions":ArtifactRef(sha256="b"*64,media_type="application/json",size_bytes=11)}))
    assert first.record_hash() != changed.record_hash()

def test_evidence_schema_exports(tmp_path):
    from app.contracts.export import export_schemas
    exported=export_schemas(tmp_path)
    assert {"evidence.json","workflow.json"}.issubset(exported)
    for name in ("evidence.json","workflow.json"):
        assert json.loads((tmp_path/name).read_text()) == json.loads((Path("schemas/v1")/name).read_text())


@pytest.mark.parametrize("kind",["interpretation","scenario","uncertainty"])
@pytest.mark.parametrize("status",["REJECTED","NEEDS_REVISION"])
def test_explicit_negative_verdict_blocks_minor_analysis(kind,status):
    claim=ClaimRevision(claim_id=UUID(int=40),revision=1,text="Minor synthetic analysis",claim_type=kind,materiality="minor")
    verdict=VerificationRecord(claim_id=claim.claim_id,claim_revision=1,claim_hash=claim.record_hash(),status=status,evidence_refs=())
    assert validate_evidence([claim],[],[verdict]).ready is False

def test_optional_claim_supplied_verdict_must_be_current(source,claim,verdict):
    minor=ClaimRevision(**(claim.model_dump()|{"claim_type":"interpretation","materiality":"minor"}))
    assert validate_evidence([minor],[source],[verdict]).ready is False


@pytest.mark.parametrize("reverse",[False,True])
def test_claim_use_duplicate_verdicts_always_block(claim,verdict,reverse):
    rejected=VerificationRecord(**(verdict.model_dump()|{"status":"REJECTED"}))
    verdicts=[rejected,verdict]
    if reverse:
        verdicts.reverse()
    use=ClaimUse(claim_id=claim.claim_id,claim_revision=1,text="Synthetic observation")
    assert validate_claim_uses([use],[claim],verdicts).ready is False

@pytest.mark.parametrize("reverse",[False,True])
def test_claim_use_duplicate_claims_always_block(claim,verdict,reverse):
    stale=ClaimRevision(**(claim.model_dump()|{"text":"Different synthetic assertion"}))
    claims=[stale,claim]
    if reverse:
        claims.reverse()
    use=ClaimUse(claim_id=claim.claim_id,claim_revision=1,text="Synthetic observation")
    assert validate_claim_uses([use],claims,[verdict]).ready is False
