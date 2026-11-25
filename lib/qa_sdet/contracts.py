"""The JSON shapes agents and qa-mem exchange, for the type checker. Descriptions only: these files come from agents,
so memory.check_run, the PostToolUse hook and the readers validate them at run time, and a value can still be any
JSON. Modules import this under TYPE_CHECKING (start-up time). Each Literal matches a runtime tuple; tests pin them
(test_properties.ContractsMatchCode). References: references/run-json.md, references/planning.md, ops.marker_start."""

from __future__ import annotations

from typing import Any, Literal, TypedDict

JsonObj = dict[str, Any]  # a parsed JSON object: agents and users write these files, any value is possible

CaseStatus = Literal["PASS", "FAIL", "BLOCKED", "NOT_RUN", "QUESTION"]  # core.CASE_STATUSES
BugStatus = Literal["OPEN", "FILED", "FIXED", "INVALID", "REGRESSED"]  # core.STATUSES
BlockScope = Literal["page", "session", "browser"]  # memory.BLOCK_SCOPES
StopReason = Literal[  # memory.STOP_REASONS
    "saturated", "charter_complete", "diminishing_returns", "turn_limit", "budget_limit", "time_limit", "blocked"
]
Verdict = Literal["FAIL", "BLOCKED", "PASS WITH NOTES", "PASS", "NOT STARTED"]  # report.VERDICT_ICON


class Case(TypedDict, total=False):  # a run file's case result
    id: str
    status: CaseStatus
    actual: str
    notes: str
    bug: str
    annotated: bool


class Question(TypedDict, total=False):
    text: str
    status: Literal["open", "resolved"]
    answer: str
    ticket: str


class NotTested(TypedDict, total=False):
    category: Literal["safety", "out_of_scope", "not_reached", "needs_setup", "heuristic", "other"]
    text: str


class Lead(TypedDict, total=False):
    route: str
    kind: str
    target: str
    reason: str
    priority: Literal["HIGH", "MEDIUM"]


class Bug(TypedDict, total=False):
    route: str
    title: str
    symptom: str
    hash: str
    severity: str
    classification: str
    draft_path: str
    evidence: str
    annotated: bool
    ticket: str
    heuristic: str
    endpoint: str
    pattern: str


class Page(TypedDict, total=False):
    route: str
    name: str
    facts: list[dict[str, str]]
    locators: list[dict[str, str]]
    coverage: list[dict[str, str]]
    states: list[dict[str, str]]
    transitions: list[dict[str, str]]


class RunFile(TypedDict, total=False):  # <run>/.data/run-<slug>.json
    run_id: str
    mode: Literal["explore", "ticket", "map"]
    target: str
    build: str
    status: Literal["COMPLETED", "INCOMPLETE", "BLOCKED"]
    block_scope: BlockScope
    stop_reason: StopReason
    leads: list[Lead]
    summary: str
    env: dict[str, str]
    cases: list[Case]
    questions: list[Question | str]
    not_tested: list[NotTested]
    notes: str
    pages: list[Page]
    bugs: list[Bug]


class Ac(TypedDict, total=False):
    id: str
    text: str


class Ticket(TypedDict, total=False):  # plan.json tickets[]
    key: str
    summary: str
    acs: list[Ac]
    plan: dict[str, object]
    fix_version: str
    fixed_note: str


class PlanCase(TypedDict, total=False):  # plan.json cases[]
    id: str
    ticket: str
    ac: str
    title: str
    route: str
    priority: str
    technique: str
    steps: list[str]
    expected: str


class Plan(TypedDict, total=False):  # <run>/.data/plan.json
    kind: Literal["ticket", "plan", "explore"]
    title: str
    created: str
    env: dict[str, str]
    tickets: list[Ticket]
    cases: list[PlanCase]
    questions: list[Question | str]
    retest_of: str


class Marker(TypedDict, total=False):  # qa-out/.active, written by ops.marker_start, read by hooks/guard.py
    expires: int
    deadline: int
    budget: int
    faults: bool
    api_writes: bool
    used: int
    run_dir: str
    slug: str
    max_turns: int
