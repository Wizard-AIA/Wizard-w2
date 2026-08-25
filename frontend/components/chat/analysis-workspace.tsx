"use client"

import {
  ChevronRight,
  ClipboardCheck,
  FlaskConical,
  Gauge,
  GitBranch,
  HelpCircle,
  Network,
  ShieldAlert,
  Target,
} from "lucide-react"
import { useState } from "react"

import type {
  AnalysisSnapshot,
  Confidence,
  ConfidenceLevel,
  CriticFinding,
  Hypothesis,
  HypothesisStatus,
  RouteComparison,
  ValidationFinding,
} from "@/lib/types"
import { cn } from "@/lib/utils"

/**
 * The analytical control plane's working state for one turn: objective,
 * hypotheses, plan revisions, validation and critic findings, competing-route
 * comparisons, confidence, open questions and provenance counts.
 *
 * Deliberately does not repeat `findings`/`assumptions`/`warnings` — those are
 * `AnswerTrust`'s. This is everything *behind* the answer's trust surface: the
 * structured reasoning that produced it, not the raw output signals. Collapsed
 * by default, and renders nothing at all when the turn produced no analytical
 * state worth a panel (e.g. a compact-tier turn, which skips this layer).
 */
export function AnalysisWorkspace({ analysis }: { analysis?: AnalysisSnapshot | null }) {
  const [open, setOpen] = useState(false)
  if (!analysis) return null

  const hasContent =
    Boolean(analysis.objective?.analyticalType) ||
    analysis.objective?.ambiguity.length ||
    analysis.hypotheses.length > 0 ||
    analysis.planRevisions.length > 1 ||
    analysis.validations.length > 0 ||
    analysis.criticFindings.length > 0 ||
    analysis.routeComparisons.length > 0 ||
    analysis.openQuestions.length > 0 ||
    Boolean(analysis.confidence) ||
    analysis.evidence.nodes.length > 0

  if (!hasContent) return null

  return (
    <div className="ring-gradient overflow-hidden rounded-xl">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        className="flex w-full items-center gap-2.5 px-3.5 py-2.5 text-left transition-colors duration-[var(--duration-fast)] hover:bg-muted/50"
      >
        <ChevronRight
          className={cn(
            "h-3.5 w-3.5 shrink-0 text-muted-foreground",
            "transition-transform duration-[var(--duration-base)] ease-[var(--ease-out-expo)]",
            open && "rotate-90",
          )}
        />
        <FlaskConical className="h-3.5 w-3.5 shrink-0 text-brand" />
        <span className="text-[12.5px] font-medium">Analysis workspace</span>
        {analysis.confidence && (
          <span className={cn("ml-auto text-[11px] font-medium", CONFIDENCE_TONE[analysis.confidence.verdict])}>
            {CONFIDENCE_LABEL[analysis.confidence.verdict]}
          </span>
        )}
      </button>

      {open && (
        <div className="space-y-2.5 border-t border-border p-3.5">
          {analysis.objective && (analysis.objective.analyticalType || analysis.objective.ambiguity.length > 0) && (
            <ObjectiveBlock objective={analysis.objective} />
          )}

          {analysis.confidence && <ConfidenceBlock confidence={analysis.confidence} />}

          {analysis.hypotheses.length > 0 && <HypothesesSection hypotheses={analysis.hypotheses} />}

          {analysis.routeComparisons.length > 0 && <RouteComparisonsSection comparisons={analysis.routeComparisons} />}

          {analysis.criticFindings.length > 0 && (
            <FindingsSection title="Critic findings" icon={ShieldAlert} findings={analysis.criticFindings} />
          )}

          {analysis.validations.length > 0 && (
            <ValidationsSection title="Validations" validations={analysis.validations} />
          )}

          {analysis.planRevisions.length > 1 && <PlanRevisionsSection revisions={analysis.planRevisions} />}

          {analysis.openQuestions.length > 0 && (
            <ListBlock title="Open questions" items={analysis.openQuestions} icon={HelpCircle} />
          )}

          {(analysis.evidence.nodes.length > 0 || analysis.evidenceRefs.length > 0) && (
            <p className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
              <Network className="h-3 w-3 shrink-0" />
              {analysis.evidence.nodes.length} evidence node(s) recorded, {analysis.evidenceRefs.length} cited in
              this answer.
            </p>
          )}
        </div>
      )}
    </div>
  )
}

const CONFIDENCE_LABEL: Record<Confidence["verdict"], string> = {
  answerable: "Answerable",
  answerable_with_caveats: "Answerable, with caveats",
  insufficient_evidence: "Insufficient evidence",
  cannot_answer: "Cannot answer",
}

const CONFIDENCE_TONE: Record<Confidence["verdict"], string> = {
  answerable: "text-success",
  answerable_with_caveats: "text-warning",
  insufficient_evidence: "text-warning",
  cannot_answer: "text-destructive",
}

const CONFIDENCE_LEVEL_TONE: Record<ConfidenceLevel, string> = {
  high: "text-success",
  medium: "text-warning",
  low: "text-destructive",
  unknown: "text-muted-foreground",
}

const HYPOTHESIS_TONE: Record<HypothesisStatus, string> = {
  supported: "text-success",
  refuted: "text-destructive",
  inconclusive: "text-warning",
  unresolved: "text-warning",
  untested: "text-muted-foreground",
}

const SEVERITY_TONE: Record<ValidationFinding["severity"], string> = {
  error: "text-destructive",
  warning: "text-warning",
  info: "text-muted-foreground",
}

function ObjectiveBlock({ objective }: { objective: NonNullable<AnalysisSnapshot["objective"]> }) {
  return (
    <div className="flex items-start gap-2 text-[12.5px] leading-relaxed">
      <Target className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground" />
      <div>
        {objective.analyticalType && (
          <p>
            <span className="font-medium">Objective:</span> {objective.analyticalType.replace(/_/g, " ")}
          </p>
        )}
        {objective.ambiguity.map((note, index) => (
          <p key={index} className="mt-0.5 text-[11.5px] italic text-muted-foreground">
            {note}
          </p>
        ))}
      </div>
    </div>
  )
}

function ConfidenceBlock({ confidence }: { confidence: Confidence }) {
  const [open, setOpen] = useState(false)
  return (
    <div>
      <button
        type="button"
        onClick={() => confidence.components.length > 0 && setOpen((value) => !value)}
        disabled={confidence.components.length === 0}
        className="flex w-full items-start gap-2 text-left text-[12.5px] leading-relaxed"
      >
        <Gauge className={cn("mt-0.5 h-3.5 w-3.5 shrink-0", CONFIDENCE_TONE[confidence.verdict])} />
        <span>
          <span className="font-medium">Confidence:</span> {CONFIDENCE_LABEL[confidence.verdict]}
          {confidence.reasons.length > 0 && (
            <span className="mt-0.5 block text-[11.5px] text-muted-foreground">{confidence.reasons.join(" ")}</span>
          )}
        </span>
        {confidence.components.length > 0 && (
          <ChevronRight
            className={cn(
              "ml-auto mt-0.5 h-3 w-3 shrink-0 text-muted-foreground transition-transform",
              open && "rotate-90",
            )}
          />
        )}
      </button>
      {open && (
        <ul className="ml-6 mt-1.5 space-y-1">
          {confidence.components.map((component) => (
            <li key={component.name} className="text-[11.5px] leading-relaxed text-muted-foreground">
              <span className={cn("font-medium", CONFIDENCE_LEVEL_TONE[component.level])}>{component.level}</span>
              {" · "}
              {component.name.replace(/_/g, " ")} — {component.reason}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function HypothesesSection({ hypotheses }: { hypotheses: Hypothesis[] }) {
  return (
    <CollapsibleSection title="Hypotheses" count={hypotheses.length} icon={FlaskConical}>
      <ul className="space-y-1.5">
        {hypotheses.map((hypothesis) => (
          <li key={hypothesis.id} className="text-[12px] leading-relaxed text-muted-foreground">
            <span className={cn("font-medium", HYPOTHESIS_TONE[hypothesis.status])}>{hypothesis.status}</span>
            {" · "}
            {hypothesis.statement}
            {(hypothesis.evidenceFor.length > 0 || hypothesis.evidenceAgainst.length > 0) && (
              <span className="ml-1 font-mono text-[10.5px]">
                ({hypothesis.evidenceFor.length} for, {hypothesis.evidenceAgainst.length} against)
              </span>
            )}
          </li>
        ))}
      </ul>
    </CollapsibleSection>
  )
}

function RouteComparisonsSection({ comparisons }: { comparisons: RouteComparison[] }) {
  return (
    <CollapsibleSection title="Competing methods" count={comparisons.length} icon={GitBranch}>
      <ul className="space-y-2">
        {comparisons.map((comparison, index) => (
          <li key={index} className="text-[12px] leading-relaxed text-muted-foreground">
            <span
              className={cn(
                "font-medium",
                comparison.verdict === "agree" && "text-success",
                comparison.verdict === "disagree" && "text-warning",
                comparison.verdict === "inconclusive" && "text-muted-foreground",
              )}
            >
              {comparison.verdict}
            </span>
            {" · "}
            {comparison.agreementDetail}
            {comparison.moreAppropriate && (
              <span className="mt-0.5 block italic">
                &quot;{comparison.moreAppropriate}&quot; is better supported — {comparison.why}
              </span>
            )}
          </li>
        ))}
      </ul>
    </CollapsibleSection>
  )
}

function FindingsSection({
  title,
  icon,
  findings,
}: {
  title: string
  icon: typeof ShieldAlert
  findings: CriticFinding[]
}) {
  return (
    <CollapsibleSection title={title} count={findings.length} icon={icon}>
      <ul className="space-y-1.5">
        {findings.map((finding, index) => (
          <li key={index} className="text-[12px] leading-relaxed text-muted-foreground">
            <span className={cn("font-medium", SEVERITY_TONE[finding.severity])}>{finding.category}</span>
            {" · "}
            {finding.message}
            {finding.detail && <span className="mt-0.5 block text-[11px] italic">{finding.detail}</span>}
          </li>
        ))}
      </ul>
    </CollapsibleSection>
  )
}

function ValidationsSection({ title, validations }: { title: string; validations: ValidationFinding[] }) {
  return (
    <CollapsibleSection title={title} count={validations.length} icon={ClipboardCheck}>
      <ul className="space-y-1.5">
        {validations.map((finding, index) => (
          <li key={index} className="text-[12px] leading-relaxed text-muted-foreground">
            <span className={cn("font-medium", SEVERITY_TONE[finding.severity])}>{finding.validator}</span>
            {" · "}
            {finding.message}
          </li>
        ))}
      </ul>
    </CollapsibleSection>
  )
}

function PlanRevisionsSection({ revisions }: { revisions: AnalysisSnapshot["planRevisions"] }) {
  return (
    <CollapsibleSection title="Plan revisions" count={revisions.length} icon={ClipboardCheck}>
      <ol className="space-y-1.5">
        {revisions.map((revision) => (
          <li key={revision.index} className="text-[12px] leading-relaxed text-muted-foreground">
            <span className="font-mono text-[10.5px]">{revision.index}.</span> {revision.text}
            {revision.why && <span className="ml-1 italic">— {revision.why}</span>}
          </li>
        ))}
      </ol>
    </CollapsibleSection>
  )
}

function ListBlock({ title, items, icon: Icon }: { title: string; items: string[]; icon: typeof HelpCircle }) {
  return (
    <div className="flex items-start gap-2 text-[12.5px] leading-relaxed">
      <Icon className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground" />
      <div>
        <p className="font-medium">{title}</p>
        <ul className="mt-1 space-y-1">
          {items.map((item, index) => (
            <li key={index} className="text-[12px] text-muted-foreground">
              {item}
            </li>
          ))}
        </ul>
      </div>
    </div>
  )
}

function CollapsibleSection({
  title,
  count,
  icon: Icon,
  children,
}: {
  title: string
  count: number
  icon: typeof ShieldAlert
  children: React.ReactNode
}) {
  const [open, setOpen] = useState(false)
  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 text-left text-[12.5px] font-medium"
      >
        <Icon className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
        {title}
        <span className="font-mono text-[10.5px] text-muted-foreground">{count}</span>
        <ChevronRight
          className={cn(
            "ml-auto h-3 w-3 shrink-0 text-muted-foreground transition-transform duration-[var(--duration-base)] ease-[var(--ease-out-expo)]",
            open && "rotate-90",
          )}
        />
      </button>
      {open && <div className="mt-1.5 pl-6">{children}</div>}
    </div>
  )
}
