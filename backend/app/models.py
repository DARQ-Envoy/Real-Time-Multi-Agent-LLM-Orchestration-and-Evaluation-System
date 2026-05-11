from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field


class AgentStartEvent(BaseModel):
    type: Literal["agent_start"] = "agent_start"
    agent_id: str
    budget_remaining: int


class TokenEvent(BaseModel):
    type: Literal["token"] = "token"
    agent_id: str
    text: str


class ToolCallStartEvent(BaseModel):
    type: Literal["tool_call_start"] = "tool_call_start"
    tool_name: str
    input_hash: str


class ToolCallEndEvent(BaseModel):
    type: Literal["tool_call_end"] = "tool_call_end"
    tool_name: str
    latency_ms: float
    success: bool


class BudgetUpdateEvent(BaseModel):
    type: Literal["budget_update"] = "budget_update"
    agent_id: str
    tokens_used: int
    tokens_remaining: int


class AgentEndEvent(BaseModel):
    type: Literal["agent_end"] = "agent_end"
    agent_id: str
    output_hash: str
    policy_violations: str | None = None


class JobCompleteEvent(BaseModel):
    type: Literal["job_complete"] = "job_complete"
    job_id: str
    total_latency_ms: float


class ErrorEvent(BaseModel):
    type: Literal["error"] = "error"
    error_code: str
    message: str
    job_id: str


class BudgetRequestEvent(BaseModel):
    type: Literal["budget_request"] = "budget_request"
    agent_id: str
    tokens_requested: int


SSEEvent = Annotated[
    Union[
        AgentStartEvent,
        TokenEvent,
        ToolCallStartEvent,
        ToolCallEndEvent,
        BudgetUpdateEvent,
        BudgetRequestEvent,
        AgentEndEvent,
        JobCompleteEvent,
        ErrorEvent,
    ],
    Field(discriminator="type"),
]


class JobRequest(BaseModel):
    query: str = Field(min_length=1)
    max_budget_tokens: int = 16384


class JobResponse(BaseModel):
    job_id: str
    stream_url: str


class PlannedToolCall(BaseModel):
    agent_id: str
    tool_name: str
    input: dict[str, Any] = Field(default_factory=dict)


class RoutingPlan(BaseModel):
    agent_sequence: list[str] = Field(default_factory=list)
    dependency_edges: list[tuple[str, str]] = Field(default_factory=list)
    budget_allocations: dict[str, int] = Field(default_factory=dict)
    tool_calls: list[PlannedToolCall] = Field(default_factory=list)
    justification: str = ""


class Chunk(BaseModel):
    chunk_id: str
    text: str
    source_url: str
    relevance_score: float | None = None
    hop_number: int | None = None


SubTaskType = Literal["FACTUAL", "ANALYTICAL", "GENERATIVE", "VERIFICATIONAL"]


class SubTask(BaseModel):
    task_id: str
    task_type: SubTaskType
    description: str
    depends_on: list[str] = Field(default_factory=list)
    priority: int = 1


class DecompositionResult(BaseModel):
    sub_tasks: list[SubTask] = Field(default_factory=list)


class SentenceProvenance(BaseModel):
    sentence_text: str
    source_agent: str
    source_chunk_ids: list[str] = Field(default_factory=list)
    contradiction_resolved: bool = False


CritiqueVerdict = Literal["SUPPORTED", "UNSUPPORTED", "UNCERTAIN"]


class ClaimReview(BaseModel):
    span_text: str
    confidence_score: float
    verdict: CritiqueVerdict
    reason: str = ""


class CritiqueReport(BaseModel):
    target_agent_id: str
    reviews: list[ClaimReview] = Field(default_factory=list)


class ContradictionSpan(BaseModel):
    turn_a: str
    turn_b: str
    description: str


class CompressedContext(BaseModel):
    compression_ratio: float
    lossless_fields_preserved: list[str] = Field(default_factory=list)
    summary: str = ""


class SharedContext(BaseModel):
    job_id: str
    query: str
    max_budget_tokens: int = 16384
    agent_outputs: dict[str, Any] = Field(default_factory=dict)
    routing_plan: RoutingPlan | None = None
    decomposition: DecompositionResult | None = None
    rag_chunks: list[Chunk] = Field(default_factory=list)
    rag_answer: str | None = None
    final_answer: list[SentenceProvenance] = Field(default_factory=list)
    low_coverage: bool = False
    critique_reports: list[CritiqueReport] = Field(default_factory=list)
    resolution_loop_active: bool = False
    compressed: CompressedContext | None = None


class ToolResult(BaseModel):
    tool_name: str
    success: bool
    data: Any | None = None
    error_code: str | None = None
    error_message: str | None = None
    latency_ms: float
    accepted_by_agent: bool | None = None
    retry_number: int = 0


class AnswerSegment(BaseModel):
    text: str
    citations: list[str] = Field(default_factory=list)
