"use client"

import {
  AlertTriangle,
  BarChart3,
  BookmarkPlus,
  Check,
  Copy,
  Download,
  TriangleAlert,
} from "lucide-react"
import { useEffect, useRef, useState } from "react"

import { AnimatedOrb } from "@/components/animated-orb"
import { MarkdownRenderer } from "@/components/markdown-renderer"
import { AnalysisWorkspace } from "@/components/chat/analysis-workspace"
import { AnswerTrust } from "@/components/chat/answer-trust"
import { InvestigationTrail } from "@/components/chat/investigation-trail"
import { ReasoningPanel } from "@/components/chat/reasoning-panel"
import { SkillCredit } from "@/components/chat/skill-credit"
import { SkillPromotion } from "@/components/chat/skill-promotion"
import { StepTimeline } from "@/components/chat/step-timeline"
import { exportUrl, workspaceFileUrl } from "@/lib/api"
import type { Artifact, ChatMessage } from "@/lib/types"
import { cn } from "@/lib/utils"

interface MessageProps {
  message: ChatMessage
  onApprove: (message: ChatMessage, approved: boolean) => void
  onOpenArtifact: (artifact: Artifact) => void
  onSkillCandidateSettled: (messageId: string) => void
}

// Openings of the two warnings that AnswerTrust renders as callouts. Kept in
// sync with `grounding.GroundingReport.warning()` and `orchestrator._verify`.
const GROUNDING_WARNING_PREFIX = "These figures in the answer"
const VERIFICATION_WARNING_PREFIX = "Independent verification disagreed"

/**
 * The affirmative button says what it will actually do.
 *
 * Keyed off the backend's own tool name rather than a union, so a category added
 * there falls back to a sentence that is still true instead of failing to build.
 */
function approvalLabel(tool: string): string {
  if (tool === "web_search") return "Allow search"
  if (tool === "execute_plan") return "Run it"
  return "Allow"
}

function CopyButton({ value }: { value: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <button
      type="button"
      onClick={() => {
        void navigator.clipboard.writeText(value).then(() => {
          setCopied(true)
          setTimeout(() => setCopied(false), 1600)
        })
      }}
      className="inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-[11px] text-muted-foreground transition-colors duration-[var(--duration-fast)] hover:bg-muted hover:text-foreground"
      aria-label="Copy message"
    >
      {copied ? <Check className="h-3 w-3 text-success" /> : <Copy className="h-3 w-3" />}
      {copied ? "Copied" : "Copy"}
    </button>
  )
}

/**
 * Re-download this turn's real executed steps as a runnable script or notebook.
 *
 * Sits with Copy and Save-as-skill rather than in the generic files list: a
 * chart PNG and a re-runnable analysis are not the same kind of download, and
 * burying the one export a user is likely to actually want among every
 * workspace file is what this milestone replaces. `messageId` is only set
 * once the `final` frame lands, which is also when there is anything to
 * export.
 */
function ExportMenu({ messageId }: { messageId: number }) {
  const [open, setOpen] = useState(false)
  const containerRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onPointerDown = (event: PointerEvent) => {
      if (!containerRef.current?.contains(event.target as Node)) setOpen(false)
    }
    document.addEventListener("pointerdown", onPointerDown)
    return () => document.removeEventListener("pointerdown", onPointerDown)
  }, [open])

  return (
    <div ref={containerRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-haspopup="dialog"
        aria-expanded={open}
        className="inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-[11px] text-muted-foreground transition-colors duration-[var(--duration-fast)] hover:bg-muted hover:text-foreground"
      >
        <Download className="h-3 w-3" />
        Export
      </button>

      {open && (
        <div
          role="dialog"
          aria-label="Export analysis"
          className="glass absolute bottom-full left-0 z-50 mb-2 w-[220px] rounded-xl border border-border p-1.5 shadow-lg reveal-scale"
        >
          <a
            href={exportUrl(messageId, "script")}
            download
            onClick={() => setOpen(false)}
            className="flex items-center gap-2 rounded-lg px-2 py-2 text-[12.5px] transition-colors duration-[var(--duration-fast)] hover:bg-accent/40"
          >
            Download script (.py)
          </a>
          <a
            href={exportUrl(messageId, "notebook")}
            download
            onClick={() => setOpen(false)}
            className="flex items-center gap-2 rounded-lg px-2 py-2 text-[12.5px] transition-colors duration-[var(--duration-fast)] hover:bg-accent/40"
          >
            Download notebook (.ipynb)
          </a>
        </div>
      )}
    </div>
  )
}

/**
 * One conversation turn.
 *
 * User turns are a right-aligned bubble; assistant turns are full-width prose,
 * matching ChatGPT/Gemini. Reasoning, tool steps and artifacts are secondary
 * surfaces around the answer rather than concatenated into it.
 */
export function Message({
  message,
  onApprove,
  onOpenArtifact,
  onSkillCandidateSettled,
}: MessageProps) {
  // Declared above the early return: the rule is that hooks run unconditionally,
  // and a user message returns before anything else happens.
  const [saving, setSaving] = useState(false)

  if (message.role === "user") {
    return (
      <div className="reveal-in flex justify-end px-4 py-2.5">
        <div className="max-w-[80%] rounded-2xl rounded-br-md bg-primary px-4 py-2.5 text-[14.5px] leading-relaxed text-primary-foreground shadow-sm">
          <p className="whitespace-pre-wrap break-words">{message.content}</p>
        </div>
      </div>
    )
  }

  const showCursor = message.streaming && message.content.length > 0
  const waitingForFirstToken = message.streaming && !message.content && !message.reasoning

  // Grounding and verification arrive both as a warning string (for REST
  // clients, which have no richer surface) and as structured fields. Rendering
  // both would say the same thing twice, so the ones AnswerTrust owns are
  // dropped from the plain list.
  const plainWarnings = message.warnings.filter(
    (warning) => !warning.startsWith(GROUNDING_WARNING_PREFIX) && !warning.startsWith(VERIFICATION_WARNING_PREFIX),
  )

  return (
    <div className="group px-4 py-3">
      <div className="flex gap-3">
        {/* The orb doubles as the assistant avatar and as the "working" indicator:
            it is always in motion, so a static frame never looks stalled. */}
        <div className="mt-0.5">
          <AnimatedOrb size={26} />
        </div>

        <div className="min-w-0 flex-1">
          {message.reasoning && (
            <ReasoningPanel
              content={message.reasoning}
              streaming={Boolean(message.streaming) && !message.content}
              elapsedMs={message.elapsedMs}
            />
          )}

          {/* The trail is what the agent *chose* to do; the timeline is the
              mechanics of each attempt. Trail first — it is the narrative. */}
          <div className="mb-3 space-y-2">
            <InvestigationTrail
              trail={message.trail}
              iteration={message.iteration}
              budget={message.iterationBudget}
              streaming={message.streaming}
              subagents={message.subagents}
            />
            <StepTimeline steps={message.steps} code={message.code} stdout={message.stdout} />
          </div>

          {message.plan && !message.content && (
            <div className="mb-3 rounded-xl border border-border bg-card p-3.5 shadow-xs">
              <p className="mb-2 font-mono text-[10px] uppercase tracking-[0.14em] text-brand">
                Proposed plan
              </p>
              <MarkdownRenderer content={message.plan} />
            </div>
          )}

          {waitingForFirstToken && (
            <div className="flex items-center gap-2 py-1" role="status" aria-label="Working">
              <span className="flex items-center gap-1">
                <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-brand/60 [animation-delay:0ms]" />
                <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-brand/60 [animation-delay:150ms]" />
                <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-brand/60 [animation-delay:300ms]" />
              </span>
              {message.statusLabel && (
                <span className="text-[12.5px] text-muted-foreground">{message.statusLabel}</span>
              )}
            </div>
          )}

          {message.content && (
            <div className="text-[15px] leading-7">
              <MarkdownRenderer content={message.content} />
              {/* Marks where the stream has reached. The text itself arrives token
                  by token from the socket — this is not a reveal animation. */}
              {showCursor && <span className="caret" aria-hidden="true" />}
            </div>
          )}

          {!message.streaming && (
            <div className="mt-3 space-y-2">
              <AnswerTrust
                verification={message.verification}
                grounding={message.grounding}
                assumptions={message.assumptions}
                findings={message.findings}
                tier={message.tier}
                iterations={message.iteration}
              />
              <AnalysisWorkspace analysis={message.analysis} />
              {/* With the trust surfaces rather than in the trail: where the
                  answer's reasoning came from is the same kind of question as
                  how far it can be trusted. */}
              <SkillCredit skills={message.skillsUsed} />
            </div>
          )}

          {message.artifacts.some((artifact) => artifact.kind.startsWith("plot")) && (
            <button
              type="button"
              onClick={() => {
                const plot = message.artifacts.find((artifact) => artifact.kind.startsWith("plot"))
                if (plot) onOpenArtifact(plot)
              }}
              className="lift mt-3 inline-flex items-center gap-2 rounded-lg border border-border bg-card px-3 py-2 text-[12.5px] font-medium shadow-xs hover:border-brand/40"
            >
              <BarChart3 className="h-3.5 w-3.5 text-brand" />
              View chart
            </button>
          )}

          {message.downloads.length > 0 && (
            <div className="mt-3 flex flex-wrap gap-1.5">
              {message.downloads.map((file) => (
                <a
                  key={file}
                  href={workspaceFileUrl(file)}
                  download={file}
                  className="inline-flex items-center gap-1.5 rounded-lg border border-border bg-card px-2.5 py-1.5 text-[12px] shadow-xs transition-colors duration-[var(--duration-fast)] hover:border-brand/40 hover:bg-accent"
                >
                  <Download className="h-3 w-3 text-muted-foreground" />
                  {file}
                </a>
              ))}
            </div>
          )}

          {plainWarnings.length > 0 && (
            <ul className="mt-3 space-y-1.5">
              {plainWarnings.map((warning, index) => (
                <li
                  key={`${index}-${warning.slice(0, 24)}`}
                  className="flex items-start gap-2 text-[12.5px] leading-relaxed text-warning"
                >
                  <TriangleAlert className="mt-0.5 h-3 w-3 shrink-0" />
                  <span>{warning}</span>
                </li>
              ))}
            </ul>
          )}

          {message.error && (
            <div className="mt-3 flex items-start gap-2.5 rounded-xl border border-destructive/25 bg-destructive/8 p-3.5 text-[13.5px] leading-relaxed text-destructive">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
              <span>{message.error}</span>
            </div>
          )}

          {message.approval && (
            <div className="ring-gradient mt-3 rounded-xl p-4 shadow-sm">
              <p className="mb-1 font-mono text-[10px] uppercase tracking-[0.14em] text-brand">
                Waiting on you
              </p>
              <p className="mb-1.5 text-[14px] leading-relaxed">{message.approval.prompt}</p>
              {message.approval.detail && (
                <p className="mb-3.5 text-[12px] leading-snug text-muted-foreground">
                  {message.approval.detail}
                </p>
              )}
              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={() => onApprove(message, true)}
                  className="rounded-lg bg-[linear-gradient(120deg,var(--brand),var(--brand-2))] px-3.5 py-2 text-[12.5px] font-medium text-brand-foreground shadow-brand transition-all duration-[var(--duration-fast)] hover:brightness-105 active:scale-[0.985]"
                >
                  {approvalLabel(message.approval.tool)}
                </button>
                <button
                  type="button"
                  onClick={() => onApprove(message, false)}
                  className="rounded-lg border border-border px-3.5 py-2 text-[12.5px] font-medium transition-colors duration-[var(--duration-fast)] hover:bg-muted"
                >
                  {/* A permission gate does not end the run, so declining is not
                      cancelling anything — the agent carries on without it. */}
                  {message.approval.id ? "Don’t allow" : "Cancel"}
                </button>
              </div>
            </div>
          )}

          {/* After the answer and its caveats, never before: this is an offer to
              save work that is already done, not something the turn waits on. */}
          {!message.streaming && message.skillCandidate && (
            <SkillPromotion
              candidate={message.skillCandidate}
              onSettled={() => onSkillCandidateSettled(message.id)}
            />
          )}

          {/* The other route into promotion: the agent offers when something has
              recurred, and this is for when the user decides on the spot. Only
              one of the two is ever on screen for a message. */}
          {saving && !message.skillCandidate && (
            <SkillPromotion instruction={message.instruction} onSettled={() => setSaving(false)} />
          )}

          {!message.streaming && message.content && (
            <div
              className={cn(
                "mt-1.5 flex items-center gap-1 opacity-0 transition-opacity",
                "group-hover:opacity-100 focus-within:opacity-100",
              )}
            >
              <CopyButton value={message.content} />
              {/* Sits with Copy rather than in a card of its own: it is a quiet
                  action on a finished answer, not something asking to be read. */}
              {message.role === "assistant" && message.instruction && !message.error && (
                <button
                  type="button"
                  onClick={() => setSaving(true)}
                  className="inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-[11px] text-muted-foreground transition-colors duration-[var(--duration-fast)] hover:bg-muted hover:text-foreground"
                >
                  <BookmarkPlus className="h-3 w-3" />
                  Save as skill
                </button>
              )}
              {message.messageId != null &&
                message.artifacts.some((artifact) => artifact.kind === "script") && (
                  <ExportMenu messageId={message.messageId} />
                )}
              {message.elapsedMs ? (
                <span className="text-[11px] text-muted-foreground/60">
                  {(message.elapsedMs / 1000).toFixed(1)}s
                </span>
              ) : null}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
