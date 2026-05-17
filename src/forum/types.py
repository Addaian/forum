from typing import Literal
from pydantic import BaseModel

# Layer 1 output
class CodeLocation(BaseModel):
    file: str
    line_start: int
    line_end: int
    module: str

class DecisionPoint(BaseModel):
    id: str
    principle: Literal["P1","P2","P3","P4","P5","P6","P7","P8","P9","P10"]
    locations: list[CodeLocation]
    subject: str
    evidence: dict
    alternatives: list[str]
    measured_impact: dict
    code_snippets: list[str]

class EvidenceBundle(BaseModel):
    repo: str
    commit_sha: str
    decision_points: list[DecisionPoint]
    graph_summary: dict
    git_summary: dict

# Layer 2 output
Verdict = Literal["HEALTHY","JUSTIFIED VIOLATION","STRUCTURAL DEBT",
                  "CRITICAL","DRIFTED","CONTESTED"]

class CellVote(BaseModel):
    cell_id: int
    red_persona: str
    blue_persona: str
    position: Literal["debt","justified"]
    confidence: float
    key_argument: str
    value_lens: dict[str, float]
    transcript: list[dict]

class TribunalResult(BaseModel):
    decision_point_id: str
    cells: list[CellVote]
    aggregate_vote: dict
    judge: dict

# Layer 3 output
class ReportArtifact(BaseModel):
    markdown: str
    headline: str
    stats: dict
