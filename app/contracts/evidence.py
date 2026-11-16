"""Evidence lineage checks prove linkage, not factual entailment."""
from typing import Annotated,Literal
from urllib.parse import urlsplit
from uuid import UUID
from pydantic import AfterValidator,Field,model_validator
from app.contracts.common import ExactDecimal,Nonempty,Sha256,StrictModel,UtcDatetime
from app.contracts.workflow import PolicyCheck,PolicyReport

def _source_url(value: str) -> str:
    parsed=urlsplit(value)
    if parsed.scheme not in {"http","https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError("source URL must be HTTP(S) without credentials")
    return value

SourceUrl=Annotated[str,AfterValidator(_source_url)]
Revision=Annotated[int,Field(ge=1)]

class SourceRecord(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    source_id: UUID
    revision: Revision
    original_url: SourceUrl
    canonical_url: SourceUrl
    publisher: Nonempty
    source_class: Literal["primary","reputable_secondary","commentary","unknown"]
    published_at: UtcDatetime | None
    publication_time_reason: Nonempty | None
    retrieved_at: UtcDatetime
    locator: Nonempty | None = None
    excerpt: Nonempty | None = None
    rights: Literal["unknown","metadata_only","excerpt_permitted"]
    usage_notes: Nonempty
    content_hash: Sha256 | None = None

    @model_validator(mode="after")
    def provenance_complete(self):
        if self.published_at is None and not self.publication_time_reason:
            raise ValueError("unknown publication time requires a reason")
        if self.excerpt is not None and self.rights != "excerpt_permitted":
            raise ValueError("excerpt retention requires explicit permission")
        if not self.locator and not self.excerpt:
            raise ValueError("source requires locator or permitted excerpt")
        return self

class NumericObservation(StrictModel):
    value: ExactDecimal
    unit: Literal["number","currency","percent","percentage_points","count"]
    currency: Annotated[str,Field(pattern=r"^[A-Z]{3}$")] | None
    period: Nonempty | None
    geography: Nonempty | None
    seasonal_adjustment: Literal["adjusted","unadjusted","unknown"]
    basis: Literal["nominal","real","not_applicable","unknown"]
    revision_status: Literal["provisional","revised","final","unknown"]
    vintage: UtcDatetime | None
    classification: Literal["actual","forecast","scenario","unknown"]
    comparison_baseline: Nonempty | None

    @model_validator(mode="after")
    def money_unit(self):
        if (self.unit == "currency") != (self.currency is not None):
            raise ValueError("currency code must match currency unit")
        return self

class EvidenceRef(StrictModel):
    source_id: UUID
    source_revision: Revision
    source_record_hash: Sha256

class ClaimRevision(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    claim_id: UUID
    revision: Revision
    text: Nonempty
    claim_type: Literal["fact","interpretation","scenario","uncertainty"]
    materiality: Literal["material","minor"]
    evidence_refs: tuple[EvidenceRef,...] = ()
    numeric_observations: tuple[NumericObservation,...] = ()
    caveats: tuple[Nonempty,...] = ()

class VerificationRecord(StrictModel):
    claim_id: UUID
    claim_revision: Revision
    claim_hash: Sha256
    status: Literal["VERIFIED","VERIFIED_WITH_CAVEAT","NEEDS_REVISION","REJECTED"]
    evidence_refs: tuple[EvidenceRef,...]
    caveat: Nonempty | None = None

    @model_validator(mode="after")
    def caveated_verdict(self):
        if self.status == "VERIFIED_WITH_CAVEAT" and not self.caveat:
            raise ValueError("caveated verification needs explicit caveat")
        return self

class ClaimUse(StrictModel):
    claim_id: UUID
    claim_revision: Revision
    text: Nonempty

def _report(checks: list[PolicyCheck]) -> PolicyReport:
    failures=tuple(check.code for check in checks if not check.passed)
    return PolicyReport(ready=bool(checks) and not failures,checks=tuple(checks),critical_failures=failures)

def validate_evidence(claims: list[ClaimRevision], sources: list[SourceRecord], verdicts: list[VerificationRecord]) -> PolicyReport:
    checks=[]
    def check(code: str,passed: bool):
        checks.append(PolicyCheck(code=code,passed=passed))
    source_map={s.source_id:s for s in sources}
    claim_map={c.claim_id:c for c in claims}
    verdict_map={v.claim_id:v for v in verdicts}
    check("nonempty_claims",bool(claims))
    check("unique_source_ids",len(source_map)==len(sources))
    check("unique_claim_ids",len(claim_map)==len(claims))
    check("unique_verdict_ids",len(verdict_map)==len(verdicts))
    check("no_orphan_verdicts",set(verdict_map).issubset(claim_map))
    def current(ref: EvidenceRef) -> bool:
        source=source_map.get(ref.source_id)
        return source is not None and source.revision==ref.source_revision and source.record_hash()==ref.source_record_hash
    for claim in claims:
        prefix=str(claim.claim_id)
        check(prefix+":current_evidence",all(current(ref) for ref in claim.evidence_refs))
        required=claim.materiality=="material" or claim.claim_type=="fact"
        verdict=verdict_map.get(claim.claim_id)
        if required:
            check(prefix+":has_evidence",bool(claim.evidence_refs))
            check(prefix+":has_verdict",verdict is not None)
        if verdict is not None:
            check(prefix+":approved_current_verdict",verdict.claim_revision==claim.revision and verdict.claim_hash==claim.record_hash() and verdict.status in {"VERIFIED","VERIFIED_WITH_CAVEAT"})
            check(prefix+":verdict_evidence",bool(verdict.evidence_refs) and all(current(ref) for ref in verdict.evidence_refs))
            check(prefix+":caveat_not_lost",not claim.caveats or verdict.status=="VERIFIED_WITH_CAVEAT")
    return _report(checks)

def validate_claim_uses(uses: list[ClaimUse], claims: list[ClaimRevision], verdicts: list[VerificationRecord]) -> PolicyReport:
    claim_map={c.claim_id:c for c in claims}
    verdict_map={v.claim_id:v for v in verdicts}
    checks=[PolicyCheck(code="nonempty_uses",passed=bool(uses)),
            PolicyCheck(code="unique_claim_ids",passed=len(claim_map)==len(claims)),
            PolicyCheck(code="unique_verdict_ids",passed=len(verdict_map)==len(verdicts)),
            PolicyCheck(code="no_orphan_verdicts",passed=set(verdict_map).issubset(claim_map))]
    for index,use in enumerate(uses):
        claim=claim_map.get(use.claim_id)
        verdict=verdict_map.get(use.claim_id)
        valid=claim is not None and verdict is not None and use.claim_revision==claim.revision==verdict.claim_revision and verdict.claim_hash==claim.record_hash() and verdict.status in {"VERIFIED","VERIFIED_WITH_CAVEAT"}
        checks.append(PolicyCheck(code=f"use_{index}:current_verified_claim",passed=valid))
        if valid:
            caveats=set(claim.caveats)
            if verdict.caveat:
                caveats.add(verdict.caveat)
            checks.append(PolicyCheck(code=f"use_{index}:caveats_retained",passed=all(caveat in use.text for caveat in caveats)))
    return _report(checks)
