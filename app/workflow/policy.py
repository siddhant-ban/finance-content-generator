"""Store-bound, fail-closed stage policies. Registry entries are trusted configuration."""
import json
from collections.abc import Callable
from datetime import datetime
from typing import Literal
from app.contracts.common import StrictModel, Nonempty, UtcDatetime, ArtifactRef
from app.contracts.evidence import (ClaimRevision, SourceRecord, VerificationRecord, ClaimUse,
                                    validate_evidence, validate_claim_uses)
from app.contracts.workflow import PolicyCheck, PolicyReport, ReviewManifest, RevisionRef
from app.storage.artifacts import ArtifactStore, ArtifactIntegrityError

MEMBERS=('dossier','verification','script','render','subtitles','captions','charts','attributions','asset_register')

def report(checks: list[PolicyCheck]) -> PolicyReport:
    failures=tuple(c.code for c in checks if not c.passed)
    return PolicyReport(ready=bool(checks) and not failures,checks=tuple(checks),critical_failures=failures)

class AssetLicense(StrictModel):
    asset_id: Nonempty
    rights: Literal['owned','licensed','public_domain','unknown']
    expires_at: UtcDatetime | None = None

class ArtifactValidation(StrictModel):
    kind: Literal['synthetic_foundation','editorial'] | None = None
    report: PolicyReport
    decision: Literal['ADVANCE','HOLD','REJECT'] | None = None
    eligible: bool = False
    claims: tuple[ClaimRevision,...] = ()
    sources: tuple[SourceRecord,...] = ()
    verdicts: tuple[VerificationRecord,...] = ()
    uses: tuple[ClaimUse,...] = ()
    licenses: tuple[AssetLicense,...] = ()

class PolicyEngine:
    """Only configured validators establish schema semantics; callers supply refs only.

    No production validators ship with foundations. Synthetic registry keys cannot
    validate editorial manifests. Validators must parse their schemas and derive
    outputs; a JSON readiness flag is never interpreted here.
    """
    def __init__(self,store: ArtifactStore):
        self.store=store
        self._validators: dict[tuple[str,str,str],Callable[[dict],ArtifactValidation]]={}

    def register(self,stage: str,schema_id: str,kind: str,validator: Callable[[dict],ArtifactValidation]) -> None:
        if kind not in {'synthetic_foundation','editorial'}:
            raise ValueError('unsupported artifact kind')
        key=(stage,schema_id,kind)
        if key in self._validators:
            raise ValueError('policy already registered')
        self._validators[key]=validator

    def validate(self,stage: str,ref: ArtifactRef,*,kind: str | None=None) -> ArtifactValidation:
        try:
            data=json.loads(self.store.read(ref))
            if not isinstance(data,dict) or (kind is not None and data.get('kind')!=kind):
                raise ValueError('artifact kind mismatch')
            handler=self._validators.get((stage,data.get('schema_id'),data.get('kind')))
            if handler is None:
                raise ValueError('no configured validator')
            result=handler(data)
            return ArtifactValidation.model_validate(result.model_dump()).model_copy(update={'kind':data['kind']})
        except (ValueError,TypeError,KeyError,ArtifactIntegrityError) as error:
            return ArtifactValidation(report=report([PolicyCheck(code=stage+':invalid_or_unknown',passed=False)]))

    def evaluate_readiness(self,manifest: ReviewManifest,now: datetime) -> PolicyReport:
        manifest=ReviewManifest.model_validate(manifest.model_dump())
        now=AssetLicense(asset_id='clock',rights='owned',expires_at=now).expires_at
        values={name:self.validate(name,getattr(manifest,name),kind=manifest.kind) for name in MEMBERS}
        checks=[PolicyCheck(code=name+':schema',passed=value.report.ready) for name,value in values.items()]
        claims=list(values['dossier'].claims)
        sources=list(values['dossier'].sources)
        verdicts=list(values['verification'].verdicts)
        checks.extend(validate_evidence(claims,sources,verdicts).checks)
        for stage in MEMBERS:
            if stage not in {'script','subtitles','captions','charts'} and not values[stage].uses:
                continue
            use_report=validate_claim_uses(list(values[stage].uses),claims,verdicts)
            checks.extend(PolicyCheck(code=stage+':'+c.code,passed=c.passed) for c in use_report.checks)
        expected_claims=tuple(RevisionRef(record_id=c.claim_id,revision=c.revision,sha256=c.record_hash()) for c in claims)
        expected_sources=tuple(RevisionRef(record_id=s.source_id,revision=s.revision,sha256=s.record_hash()) for s in sources)
        def same_refs(left,right):
            return len(left)==len(right) and {x.canonical_bytes() for x in left}=={x.canonical_bytes() for x in right}
        checks.append(PolicyCheck(code='manifest:current_claims',passed=same_refs(manifest.claim_revision_refs,expected_claims)))
        checks.append(PolicyCheck(code='manifest:current_sources',passed=same_refs(manifest.source_revision_refs,expected_sources)))
        licenses=values['asset_register'].licenses
        checks.append(PolicyCheck(code='assets:nonempty_rights',passed=bool(licenses)))
        for stage,value in values.items():
            checks.append(PolicyCheck(code=stage+':valid_rights',passed=all(x.rights!='unknown' and (x.expires_at is None or x.expires_at>now) for x in value.licenses)))
        return report(checks)
