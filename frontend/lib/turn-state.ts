import type {
  ActionKind,
  AnalysisMode,
  AnalysisSnapshot,
  ApprovalRequest,
  Artifact,
  ChatMessage,
  Confidence,
  ConfidenceComponent,
  CriticFinding,
  Grounding,
  Hypothesis,
  Phase,
  RouteComparison,
  RunStep,
  ServerEvent,
  SkillCandidate,
  SkillUse,
  SubagentBranch,
  TrailEntry,
  ValidationFinding,
  Verification,
  TurnRoute
} from "./types"

export function newId(): string {
  if (typeof crypto !== "undefined" && crypto.randomUUID) return crypto.randomUUID()
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 9)}`
}

const EMPTY_MESSAGE = {
  steps: [],
  artifacts: [],
  warnings: [],
  downloads: [],
  trail: [],
  findings: [],
  assumptions: [],
  skillsUsed: [],
  subagents: {},
}

/** A message that has just been sent and has heard nothing back yet. */
export function blankAssistant(): ChatMessage {
  return {
    id: newId(),
    role: "assistant",
    content: "",
    createdAt: Date.now(),
    ...structuredClone(EMPTY_MESSAGE),
    streaming: true,
    // Nothing has been decided yet. "planning" here flashed a Planning row on
    // every greeting; the backend says what it is doing as soon as it knows.
    phase: "routing",
  }
}

export function blankUser(content: string): ChatMessage {
  return {
    id: newId(),
    role: "user",
    content,
    createdAt: Date.now(),
    ...structuredClone(EMPTY_MESSAGE),
  }
}

function blankAnalysis(): AnalysisSnapshot {
  return {
    objective: null,
    planRevisions: [],
    hypotheses: [],
    evidence: { nodes: [], edges: [] },
    evidenceRefs: [],
    validations: [],
    criticFindings: [],
    routeComparisons: [],
    openQuestions: [],
    confidence: null,
  }
}

export function parseAnalysisSnapshot(raw: unknown): AnalysisSnapshot {
  const data = (raw ?? {}) as Record<string, unknown>
  const objective = data.objective as Record<string, unknown> | null | undefined
  const plan = (data.plan ?? {}) as Record<string, unknown>
  const evidence = (data.evidence ?? {}) as Record<string, unknown>
  const confidence = data.confidence as Record<string, unknown> | null | undefined

  return {
    objective: objective
      ? {
          question: String(objective.question ?? ""),
          analyticalType: (objective.analytical_type as string | null) ?? null,
          unitOfAnalysis: (objective.unit_of_analysis as string | null) ?? null,
          population: (objective.population as string | null) ?? null,
          timeDimension: (objective.time_dimension as string | null) ?? null,
          likelyVariables: (objective.likely_variables as Record<string, string[]>) ?? {},
          constraints: (objective.constraints as string[]) ?? [],
          expectedOutput: (objective.expected_output as string | null) ?? null,
          ambiguity: (objective.ambiguity as string[]) ?? [],
        }
      : null,
    planRevisions: ((plan.revisions as Record<string, unknown>[]) ?? []).map((revision) => ({
      index: Number(revision.index ?? 0),
      text: String(revision.text ?? ""),
      why: String(revision.why ?? ""),
      at: Number(revision.at ?? 0),
    })),
    hypotheses: ((data.hypotheses as Record<string, unknown>[]) ?? []).map((hypothesis) => ({
      id: String(hypothesis.id ?? ""),
      kind: (hypothesis.kind as Hypothesis["kind"]) ?? "exploratory",
      statement: String(hypothesis.statement ?? ""),
      status: (hypothesis.status as Hypothesis["status"]) ?? "untested",
      evidenceFor: (hypothesis.evidence_for as string[]) ?? [],
      evidenceAgainst: (hypothesis.evidence_against as string[]) ?? [],
    })),
    evidence: {
      nodes: ((evidence.nodes as Record<string, unknown>[]) ?? []).map((node) => ({
        id: String(node.id ?? ""),
        kind: String(node.kind ?? ""),
        label: String(node.label ?? ""),
        at: Number(node.at ?? 0),
      })),
      edges: ((evidence.edges as Record<string, unknown>[]) ?? []).map((edge) => ({
        source: String(edge.source ?? ""),
        target: String(edge.target ?? ""),
        relation: String(edge.relation ?? ""),
      })),
    },
    evidenceRefs: (data.evidence_refs as string[]) ?? [],
    validations: ((data.validations as Record<string, unknown>[]) ?? []).map((finding) => ({
      validator: String(finding.validator ?? ""),
      severity: (finding.severity as ValidationFinding["severity"]) ?? "info",
      message: String(finding.message ?? ""),
      detail: (finding.detail as string) || undefined,
    })),
    criticFindings: ((data.critic_findings as Record<string, unknown>[]) ?? []).map((finding) => ({
      category: String(finding.category ?? ""),
      severity: (finding.severity as CriticFinding["severity"]) ?? "info",
      message: String(finding.message ?? ""),
      detail: (finding.detail as string) || undefined,
      suggestedReaction: (finding.suggested_reaction as string) || undefined,
    })),
    routeComparisons: ((data.route_comparisons as Record<string, unknown>[]) ?? []).map((comparison) => ({
      verdict: (comparison.verdict as RouteComparison["verdict"]) ?? "inconclusive",
      routes: (comparison.routes as string[]) ?? [],
      agreementDetail: String(comparison.agreement_detail ?? ""),
      moreAppropriate: (comparison.more_appropriate as string | null) ?? null,
      why: String(comparison.why ?? ""),
      residualUncertainty: String(comparison.residual_uncertainty ?? ""),
    })),
    openQuestions: (data.open_questions as string[]) ?? [],
    confidence: confidence
      ? {
          verdict: (confidence.verdict as Confidence["verdict"]) ?? "insufficient_evidence",
          components: ((confidence.components as Record<string, unknown>[]) ?? []).map((component) => ({
            name: String(component.name ?? ""),
            level: (component.level as ConfidenceComponent["level"]) ?? "unknown",
            reason: String(component.reason ?? ""),
          })),
          reasons: (confidence.reasons as string[]) ?? [],
          stop: confidence.stop
            ? {
                reason: String((confidence.stop as Record<string, unknown>).reason ?? ""),
                detail: String((confidence.stop as Record<string, unknown>).detail ?? ""),
              }
            : undefined,
        }
      : null,
  }
}

export function applyBranchEvent(message: ChatMessage, event: ServerEvent, branch: string): ChatMessage {
  const existing = message.subagents[branch]
  const group = String(event.group ?? existing?.group ?? "")
  const current: SubagentBranch = existing ?? { id: branch, goal: "", group, trail: [], done: false }
  let next: SubagentBranch = current

  switch (event.type) {
    case "subagent_start":
      next = { ...current, goal: String(event.goal ?? ""), group }
      break

    case "subagent_end":
      next = {
        ...current,
        done: true,
        ok: Boolean(event.ok),
        costUsd: (event.cost_usd as number | null | undefined) ?? null,
        totalTokens: Number(event.total_tokens ?? 0),
        calls: Number(event.calls ?? 0),
      }
      break

    case "iteration_start":
      next = { ...current, iteration: Number(event.n ?? 0), iterationBudget: Number(event.budget ?? 0) }
      break

    case "action": {
      const entry: TrailEntry = {
        id: newId(),
        iteration: current.iteration ?? current.trail.length + 1,
        kind: (event.kind as ActionKind) ?? "code",
        goal: String(event.goal ?? ""),
        rationale: (event.rationale as string) || undefined,
        inferred: Boolean(event.inferred),
      }
      next = { ...current, trail: [...current.trail, entry] }
      break
    }

    case "observation": {
      const trail = [...current.trail]
      for (let index = trail.length - 1; index >= 0; index -= 1) {
        if (trail[index].observation === undefined) {
          trail[index] = {
            ...trail[index],
            observation: String(event.summary ?? ""),
            ok: Boolean(event.ok),
            truncated: Boolean(event.truncated),
            chars: Number(event.chars ?? 0),
            group: (event.group as string) || trail[index].group,
          }
          break
        }
      }
      next = { ...current, trail }
      break
    }

    case "status":
      next = { ...current, statusLabel: String(event.content ?? ""), phase: (event.phase as Phase) ?? current.phase }
      break

    case "code":
      next = { ...current, code: String(event.content ?? "") }
      break

    case "stdout":
      next = { ...current, stdout: (current.stdout ?? "") + String(event.content ?? "") }
      break

    default:
      break
  }

  return { ...message, subagents: { ...message.subagents, [branch]: next } }
}

export function artifactFromEvent(event: ServerEvent): Artifact {
  return {
    kind: event.kind as Artifact["kind"],
    name: event.name as string | undefined,
    data: event.data as string | undefined,
    text: event.text as string | undefined,
  }
}

export function mergeSkills(existing: SkillUse[], names: string[]): SkillUse[] {
  const seen = new Set(existing.map((skill) => skill.name))
  const extra = names
    .filter((name) => name && !seen.has(name))
    .map((name): SkillUse => ({ name, layer: "user" }))
  return extra.length ? [...existing, ...extra] : existing
}

export interface TurnState {
  message: ChatMessage
  isRunning: boolean
  globalPhase: Phase
}

function parseTurnRoute(raw: unknown): TurnRoute | null {
  if (!raw) return null
  const data = raw as Record<string, unknown>
  return {
    workflow: (data.workflow as TurnRoute["workflow"]) ?? "converse",
    intent: (data.intent as TurnRoute["intent"]) ?? "conversation",
    plan: (data.plan as TurnRoute["plan"]) ?? "none",
    source: (data.source as TurnRoute["source"]) ?? "rules",
    complexity: String(data.complexity ?? ""),
    verify: Boolean(data.verify),
    escalate: Boolean(data.escalate),
    deep: Boolean(data.deep),
    needs_data: Boolean(data.needs_data),
    reasons: (data.reasons as string[]) ?? [],
    mode: String(data.mode ?? ""),
  }
}

export function reduceTurnState(state: TurnState, event: ServerEvent): TurnState {
  // Stale frames after a terminal frame are ignored
  if (!state.isRunning && !state.message.streaming) {
    return state
  }

  const branch = typeof event.branch === "string" ? event.branch : ""
  if (branch) {
    const nextMessage = applyBranchEvent(state.message, event, branch)
    return { ...state, message: nextMessage }
  }

  const { message } = state
  let nextMessage = { ...message }
  let nextIsRunning = state.isRunning
  let nextGlobalPhase = state.globalPhase

  switch (event.type) {
    case "status": {
      const nextPhase = (event.phase as Phase) ?? "idle"
      nextGlobalPhase = nextPhase
      nextMessage = {
        ...nextMessage,
        phase: nextPhase,
        statusLabel: String(event.content ?? ""),
      }
      break
    }

    case "route": {
      nextMessage.route = parseTurnRoute(event)
      break
    }

    case "step_start": {
      const step: RunStep = {
        id: String(event.id),
        label: String(event.label ?? ""),
        kind: (event.kind as RunStep["kind"]) ?? "plan",
        status: "running",
      }
      nextMessage = { ...nextMessage, steps: [...nextMessage.steps, step] }
      break
    }

    case "step_end": {
      nextMessage = {
        ...nextMessage,
        steps: nextMessage.steps.map((step) =>
          step.id === String(event.id)
            ? {
                ...step,
                status: event.ok ? "done" : "failed",
                durationMs: Number(event.duration_ms ?? 0),
              }
            : step,
        ),
      }
      break
    }

    case "reasoning_delta":
      nextMessage = {
        ...nextMessage,
        reasoning: (nextMessage.reasoning ?? "") + String(event.content ?? ""),
      }
      break

    case "plan_delta":
      nextMessage = {
        ...nextMessage,
        plan: (nextMessage.plan ?? "") + String(event.content ?? ""),
      }
      break

    case "content_delta":
      nextMessage = {
        ...nextMessage,
        content: nextMessage.content + String(event.content ?? ""),
      }
      break

    case "code":
      nextMessage = { ...nextMessage, code: String(event.content ?? "") }
      break

    case "stdout":
      nextMessage = {
        ...nextMessage,
        stdout: (nextMessage.stdout ?? "") + String(event.content ?? ""),
      }
      break

    case "artifact": {
      nextMessage = { ...nextMessage, artifacts: [...nextMessage.artifacts, artifactFromEvent(event)] }
      break
    }

    case "warning":
      nextMessage = {
        ...nextMessage,
        warnings: [...nextMessage.warnings, String(event.content ?? "")],
      }
      break

    case "usage":
      break

    case "iteration_start":
      nextMessage = {
        ...nextMessage,
        iteration: Number(event.n ?? 0),
        iterationBudget: Number(event.budget ?? 0),
        mode: (event.mode as AnalysisMode) ?? nextMessage.mode,
      }
      break

    case "action": {
      const entry: TrailEntry = {
        id: newId(),
        iteration: 0,
        kind: (event.kind as ActionKind) ?? "code",
        goal: String(event.goal ?? ""),
        rationale: (event.rationale as string) || undefined,
        inferred: Boolean(event.inferred),
      }
      nextMessage = {
        ...nextMessage,
        trail: [...nextMessage.trail, { ...entry, iteration: nextMessage.iteration ?? nextMessage.trail.length + 1 }],
      }
      break
    }

    case "observation": {
      const trail = [...nextMessage.trail]
      for (let index = trail.length - 1; index >= 0; index -= 1) {
        if (trail[index].observation === undefined) {
          trail[index] = {
            ...trail[index],
            observation: String(event.summary ?? ""),
            ok: Boolean(event.ok),
            truncated: Boolean(event.truncated),
            chars: Number(event.chars ?? 0),
            group: (event.group as string) || trail[index].group,
          }
          break
        }
      }
      nextMessage = { ...nextMessage, trail }
      break
    }

    case "finding":
      nextMessage = {
        ...nextMessage,
        findings: [...nextMessage.findings, String(event.text ?? "")],
      }
      break

    case "assumption": {
      const text = String(event.text ?? "")
      if (!nextMessage.assumptions.includes(text)) {
        nextMessage = { ...nextMessage, assumptions: [...nextMessage.assumptions, text] }
      }
      break
    }

    case "plan_revised": {
      const plan = String(event.plan ?? "")
      const why = String(event.why ?? "")
      nextMessage = {
        ...nextMessage,
        plan,
        findings: why && !nextMessage.findings.includes(why) ? [...nextMessage.findings, why] : nextMessage.findings,
      }
      break
    }

    case "skill": {
      const use: SkillUse = {
        name: String(event.name ?? ""),
        description: event.description as string | undefined,
        layer: (event.layer as SkillUse["layer"]) ?? "user",
        score: typeof event.score === "number" ? event.score : undefined,
        phase: event.phase as string | undefined,
      }
      if (!nextMessage.skillsUsed.some((existing) => existing.name === use.name)) {
        nextMessage = { ...nextMessage, skillsUsed: [...nextMessage.skillsUsed, use] }
      }
      break
    }

    case "skill_candidate":
      nextMessage = {
        ...nextMessage,
        skillCandidate: {
          id: Number(event.id ?? 0),
          kind: (event.kind as SkillCandidate["kind"]) ?? "recurring",
          label: String(event.label ?? ""),
          instruction: String(event.instruction ?? ""),
          occurrences: Number(event.occurrences ?? 0),
          threshold: Number(event.threshold ?? 0),
          suggested_name: String(event.suggested_name ?? ""),
          plan: event.plan as string | undefined,
          code: event.code as string | undefined,
        },
      }
      break

    case "verification": {
      const verification: Verification = {
        status: (event.status as Verification["status"]) ?? "inconclusive",
        detail: String(event.detail ?? ""),
      }
      nextMessage = { ...nextMessage, verification }
      break
    }

    case "critic_finding": {
      const finding: CriticFinding = {
        category: String(event.category ?? ""),
        severity: (event.severity as CriticFinding["severity"]) ?? "info",
        message: String(event.message ?? ""),
        suggestedReaction: (event.suggested_reaction as string) || undefined,
      }
      nextMessage = {
        ...nextMessage,
        analysis: {
          ...(nextMessage.analysis ?? blankAnalysis()),
          criticFindings: [...(nextMessage.analysis?.criticFindings ?? []), finding],
        },
      }
      break
    }

    case "route_comparison": {
      const comparison: RouteComparison = {
        group: (event.group as string) || undefined,
        verdict: (event.verdict as RouteComparison["verdict"]) ?? "inconclusive",
        routes: (event.routes as string[]) ?? [],
        agreementDetail: String(event.agreement_detail ?? ""),
        moreAppropriate: (event.more_appropriate as string | null) ?? null,
        why: String(event.why ?? ""),
        residualUncertainty: String(event.residual_uncertainty ?? ""),
      }
      nextMessage = {
        ...nextMessage,
        analysis: {
          ...(nextMessage.analysis ?? blankAnalysis()),
          routeComparisons: [...(nextMessage.analysis?.routeComparisons ?? []), comparison],
        },
      }
      break
    }

    case "confidence": {
      const confidence: Confidence = {
        verdict: (event.verdict as Confidence["verdict"]) ?? "insufficient_evidence",
        components: ((event.components as Record<string, unknown>[]) ?? []).map((component) => ({
          name: String(component.name ?? ""),
          level: (component.level as ConfidenceComponent["level"]) ?? "unknown",
          reason: String(component.reason ?? ""),
        })),
        reasons: (event.reasons as string[]) ?? [],
        stop: event.stop_reason
          ? { reason: String(event.stop_reason ?? ""), detail: String(event.stop_detail ?? "") }
          : undefined,
      }
      nextMessage = {
        ...nextMessage,
        analysis: { ...(nextMessage.analysis ?? blankAnalysis()), confidence },
      }
      break
    }

    case "approval_required": {
      const approval: ApprovalRequest = {
        tool: (event.tool as string) ?? "execute_plan",
        prompt: String(event.prompt ?? "Confirm to continue."),
        plan: event.plan as string | undefined,
        query: event.query as string | undefined,
        id: event.id as string | undefined,
        category: event.category as string | undefined,
        subject: event.subject as string | undefined,
        detail: event.detail as string | undefined,
      }
      nextMessage = {
        ...nextMessage,
        approval,
        plan: (event.plan as string) ?? nextMessage.plan,
        streaming: false,
        phase: "awaiting_approval",
      }
      nextGlobalPhase = "awaiting_approval"
      if (!approval.id) {
        nextIsRunning = false
      }
      break
    }

    case "error": {
      const code = (event.code as string) ?? undefined
      const text = String(event.content ?? "Something went wrong.")
      
      if (code !== "busy") {
        nextMessage = {
          ...nextMessage,
          error: text, errorCode: code,
          streaming: false,
          phase: "failed",
        }
        nextIsRunning = false
        nextGlobalPhase = "idle"
      }
      break
    }

    case "cancelled": {
      nextMessage = {
        ...nextMessage,
        streaming: false,
        phase: "cancelled",
      }
      nextIsRunning = false
      nextGlobalPhase = "idle"
      break
    }

    case "final": {
      const finalText = String(event.response ?? "")
      nextMessage = {
        ...nextMessage,
        content: nextMessage.content || finalText,
        code: (event.code as string) || nextMessage.code,
        downloads: (event.downloads as string[]) ?? [],
        warnings: Array.from(
          new Set([...nextMessage.warnings, ...(((event.warnings as string[]) ?? []) || [])]),
        ),
        findings: Array.from(
          new Set([...nextMessage.findings, ...(((event.findings as string[]) ?? []) || [])]),
        ),
        assumptions: Array.from(
          new Set([...nextMessage.assumptions, ...(((event.assumptions as string[]) ?? []) || [])]),
        ),
        grounding: (event.grounding as Grounding) ?? nextMessage.grounding,
        skillsUsed: mergeSkills(nextMessage.skillsUsed, (event.skills_used as string[]) ?? []),
        iteration: Number(event.iterations ?? nextMessage.iteration ?? 0),
        tier: (event.tier as string) ?? nextMessage.tier,
        elapsedMs: Number(event.elapsed_ms ?? 0),
        messageId: (event.message_id as number | null | undefined) ?? nextMessage.messageId ?? null,
        analysis: event.analysis ? parseAnalysisSnapshot(event.analysis) : nextMessage.analysis,
        streaming: false,
        phase: "done",
      }
      if (event.route) {
        nextMessage.route = parseTurnRoute(event.route)
      }
      nextIsRunning = false
      nextGlobalPhase = "idle"
      break
    }

    default:
      break
  }

  return { message: nextMessage, isRunning: nextIsRunning, globalPhase: nextGlobalPhase }
}
