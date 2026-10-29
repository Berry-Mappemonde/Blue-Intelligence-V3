# Python port of the Meterless H-MEM reference (engines/hmem/reference), Apache-2.0
"""JSON-compatible records retain the reference's camelCase field names."""
DAY_MS = 86_400_000
DEFAULTS = dict(retrievalTopN=5, retrievalThreshold=.35, consolidationDays=7,
                archiveMinDays=30, archiveMaxAccess=2, synthesisCosine=.82,
                dreamClusterMinSize=2, dreamRelatedness=.35, maxDreamProposalsPerType=8,
                conflictAutoResolveGate=.70, entityCap=10, relatedCap=5, recencyDays=14)
ACTIONS = frozenset(('create read update delete feedback promote demote merge '
    'conflict_detected conflict_resolved sleep_consolidate sleep_archive '
    'sleep_synthesize dream_proposed dream_approved dream_rejected restore').split())

def clamp01(value):
    return max(0, min(1, value))


# TypedDicts describe the JSON wire format without adding runtime dependencies.
from typing import Literal, NotRequired, TypedDict

MemoryType = Literal['personal', 'factual', 'preference', 'general']
MemoryLayer = Literal['short_term', 'working', 'long_term']
FeedbackKind = Literal['helpful', 'not_helpful', 'wrong']
FEEDBACK_DELTAS = {'helpful': .05, 'not_helpful': -.03, 'wrong': -.2}


class Provenance(TypedDict):
    origin: str
    learnedAt: float
    label: str
    refs: NotRequired[list[str]]


class MemoryRecord(TypedDict):
    id: str
    content: str
    type: MemoryType
    layer: MemoryLayer
    timestamp: float
    lastAccessed: float
    accessCount: int
    tags: list[str]
    embedding: list[float]
    score: NotRequired[float]
    confidence: float
    source: str
    provenance: Provenance
    chatId: NotRequired[str]
    missionId: NotRequired[str]
    goalRunId: NotRequired[str]
    domain: str
    namespace: str
    entities: list[str]
    relatedTo: list[str]
    supersedes: NotRequired[str]
    supersededBy: NotRequired[str]
    derivedFrom: NotRequired[list[str]]


class LedgerEntry(TypedDict):
    memoryId: str
    action: str
    actor: str
    timestamp: float
    previousState: NotRequired[dict]
    newState: NotRequired[dict]
    details: NotRequired[dict]


class DreamProposal(TypedDict):
    id: str
    type: Literal['insight', 'domain', 'invariant']
    content: str
    derivedFrom: list[str]
    suggestedDomain: NotRequired[str]
    status: Literal['proposed', 'approved', 'rejected']


class SleepPreview(TypedDict):
    toConsolidate: list[str]
    toArchive: list[str]
    toSynthesize: list[list[str]]


class SleepReport(TypedDict):
    backupId: str
    consolidated: int
    archived: int
    synthesized: int
    actionLog: list[str]


class ConflictRecord(TypedDict):
    id: str
    memoryA: str
    memoryB: str
    reason: str
    confidence: float
    resolved: bool
    resolution: NotRequired[str]


class RetrievedMemory(TypedDict):
    memory: MemoryRecord
    relevance: float


class QueryResult(TypedDict):
    memories: list[RetrievedMemory]
    context: str
    trace: dict
