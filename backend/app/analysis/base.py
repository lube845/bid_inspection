from dataclasses import dataclass, field


@dataclass
class EvidenceDraft:
    evidence_type: str
    subtype: str
    severity: str
    summary: str
    evidence_data: dict = field(default_factory=dict)
    source_a: dict | None = None
    source_b: dict | None = None
