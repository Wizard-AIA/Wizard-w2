"use client"

import { AlertTriangle, Database, PanelRight, Plus, ShieldAlert, WifiOff } from "lucide-react"
import { useCallback, useEffect, useRef, useState } from "react"

import { AnimatedOrb } from "@/components/animated-orb"
import { ArtifactsPanel } from "@/components/chat/artifacts-panel"
import { Composer } from "@/components/chat/composer"
import { Message } from "@/components/chat/message"
import { ModelPicker } from "@/components/chat/model-picker"
import type { AnalysisMode, Artifact } from "@/lib/types"
import { useSound } from "@/lib/use-sound"
import { cn } from "@/lib/utils"
import { useWorkspace } from "@/lib/workspace-context"

/*
  Openers are phrased as things an analyst would actually type, and each one
  exercises a different capability — profiling, distributions, relationships,
  anomalies — so the first click also demonstrates the range.
*/
const OPENERS = [
  {
    title: "Profile this dataset",
    prompt: "Summarise this dataset and flag anything that looks like a data quality problem",
    hint: "Types, ranges, missingness",
  },
  {
    title: "Show me the shape of it",
    prompt: "Plot the distribution of every numeric column and describe what each one looks like",
    hint: "Distributions, skew, tails",
  },
  {
    title: "Find what moves together",
    prompt: "Which columns are most strongly correlated, and is any of it likely to be spurious?",
    hint: "Correlation, with caveats",
  },
  {
    title: "Show me the odd ones",
    prompt: "Find the outliers, explain what makes each one unusual, and tell me if they look like errors",
    hint: "Anomalies, explained",
  },
] as const

export function ChatShell() {
  const [mode, setMode] = useState<AnalysisMode>("auto")
  const scrollRef = useRef<HTMLDivElement>(null)
  const pinnedToBottom = useRef(true)
  const { playSound } = useSound()

  const {
    config,
    datasets,
    activeDataset,
    activeSummary,
    uploading,
    uploadError,
    panelOpen,
    setPanelOpen,
    panelTab,
    setPanelTab,
    chartVersion,
    chartImage,
    uploadDataset,
    activateDataset,
    newChat,
    chat,
  } = useWorkspace()

  const {
    messages,
    connection,
    isRunning,
    sendMessage,
    respondToApproval,
    clearSkillCandidate,
    cancel,
  } = chat

  const onArtifact = useCallback((artifact: Artifact) => {
    if (artifact.kind === "plot_html" || artifact.kind === "plot_png") {
      setPanelTab("chart")
      setPanelOpen(true)
    }
  }, [setPanelOpen, setPanelTab])

  // Follow the stream, but stop fighting the user if they scroll up to read.
  useEffect(() => {
    const node = scrollRef.current
    if (node && pinnedToBottom.current) {
      node.scrollTop = node.scrollHeight
    }
  }, [messages])

  const handleScroll = useCallback(() => {
    const node = scrollRef.current
    if (!node) return
    pinnedToBottom.current = node.scrollHeight - node.scrollTop - node.clientHeight < 120
  }, [])

  const handleSend = useCallback(
    (content: string, sendMode: AnalysisMode) => {
      playSound("click")
      sendMessage(content, sendMode)
    },
    [playSound, sendMessage],
  )

  const hasData = Boolean(activeDataset)

  return (
    <div className="flex min-h-0 flex-1 flex-col text-foreground">
      <header className="glass flex h-14 shrink-0 items-center justify-between border-b border-border px-3 sm:px-4">
        <div className="flex min-w-0 items-center gap-1 pl-11 md:pl-0">
          <button
            type="button"
            onClick={() => void newChat()}
            className="flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-[13px] font-medium text-muted-foreground transition-colors duration-[var(--duration-fast)] hover:bg-muted hover:text-foreground"
          >
            <Plus className="h-3.5 w-3.5" />
            <span className="hidden sm:inline">New</span>
          </button>

          {activeSummary && (
            <button
              type="button"
              onClick={() => {
                setPanelTab("data")
                setPanelOpen(true)
              }}
              className="group flex min-w-0 items-center gap-2 rounded-lg border border-border bg-card px-2.5 py-1.5 text-[13px] shadow-xs transition-colors duration-[var(--duration-fast)] hover:border-brand/40"
              title={`${activeSummary.rows.toLocaleString()} rows × ${activeSummary.column_count} columns`}
            >
              <Database className="h-3.5 w-3.5 shrink-0 text-success" />
              <span className="max-w-[180px] truncate font-medium">{activeSummary.name}</span>
              <span className="tabular hidden shrink-0 text-[11.5px] text-muted-foreground sm:inline">
                {activeSummary.rows.toLocaleString()} × {activeSummary.column_count}
              </span>
            </button>
          )}
        </div>

        <div className="flex items-center gap-1">
          {connection !== "open" && (
            <span
              className="flex items-center gap-1.5 rounded-lg bg-warning/12 px-2.5 py-1 text-[11.5px] font-medium text-warning"
              role="status"
            >
              <WifiOff className="h-3 w-3" />
              <span className="hidden sm:inline">
                {connection === "connecting" ? "Connecting" : "Reconnecting"}
              </span>
            </span>
          )}

          {config && !config.sandbox_available && (
            <span
              className="flex items-center gap-1.5 rounded-lg bg-warning/12 px-2.5 py-1 text-[11.5px] font-medium text-warning"
              title="Docker is unreachable, so generated code runs in a restricted in-process interpreter with weaker isolation."
            >
              <ShieldAlert className="h-3 w-3" />
              <span className="hidden sm:inline">No sandbox</span>
            </span>
          )}

          <ModelPicker />

          <button
            type="button"
            onClick={() => setPanelOpen(!panelOpen)}
            aria-label="Toggle workspace panel"
            aria-pressed={panelOpen}
            className={cn(
              "flex h-8 w-8 items-center justify-center rounded-lg transition-colors duration-[var(--duration-fast)]",
              panelOpen ? "bg-muted text-foreground" : "text-muted-foreground hover:bg-muted",
            )}
          >
            <PanelRight className="h-4 w-4" />
          </button>
        </div>
      </header>

      <div
        className={cn(
          "flex min-h-0 flex-1 flex-col transition-[margin] duration-[var(--duration-slow)] ease-[var(--ease-out-expo)]",
          panelOpen && "lg:mr-[560px]",
        )}
      >
        <div ref={scrollRef} onScroll={handleScroll} className="min-h-0 flex-1 overflow-y-auto">
          <div className="mx-auto w-full max-w-3xl pb-6 pt-4">
            {messages.length === 0 ? (
              <EmptyState
                hasData={hasData}
                datasetName={activeSummary?.name ?? null}
                onPick={(prompt) => handleSend(prompt, mode)}
                formats={config?.supported_formats ?? ["csv"]}
              />
            ) : (
              messages.map((message) => (
                <Message
                  key={message.id}
                  message={message}
                  onApprove={respondToApproval}
                  onOpenArtifact={onArtifact}
                  onSkillCandidateSettled={clearSkillCandidate}
                />
              ))
            )}

            {uploadError && (
              <div className="mx-4 mt-3 flex items-start gap-2.5 rounded-xl border border-destructive/25 bg-destructive/8 p-3.5 text-sm text-destructive">
                <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
                <span>{uploadError}</span>
              </div>
            )}
          </div>
        </div>

        <Composer
          onSend={handleSend}
          onStop={cancel}
          onUpload={(file) => void uploadDataset(file)}
          isRunning={isRunning}
          isUploading={uploading}
          hasData={hasData}
          acceptedFormats={config?.supported_formats ?? ["csv"]}
          mode={mode}
          onModeChange={setMode}
        />
      </div>

      <ArtifactsPanel
        open={panelOpen}
        onClose={() => setPanelOpen(false)}
        tab={panelTab}
        onTabChange={setPanelTab}
        chartVersion={chartVersion}
        chartImage={chartImage}
        datasets={datasets}
        activeDataset={activeDataset}
        onActivateDataset={(name) => void activateDataset(name)}
      />
    </div>
  )
}

/* -------------------------------------------------------------------------- */

function EmptyState({
  hasData,
  datasetName,
  onPick,
  formats,
}: {
  hasData: boolean
  datasetName: string | null
  onPick: (prompt: string) => void
  formats: string[]
}) {
  return (
    <div className="flex flex-col items-center px-6 py-14 text-center sm:py-20">
      <div className="reveal-scale mb-8">
        <AnimatedOrb size={64} className="float-slow" />
      </div>

      <h1
        className="reveal text-balance text-[clamp(1.75rem,4.5vw,2.5rem)] font-semibold leading-[1.05] tracking-[-0.035em]"
        style={{ animationDelay: "120ms" }}
      >
        {hasData ? "What do you want to know?" : "Bring me some data."}
      </h1>

      <p
        className="reveal mt-4 max-w-md text-pretty text-[15px] leading-relaxed text-muted-foreground"
        style={{ animationDelay: "200ms" }}
      >
        {hasData ? (
          <>
            <span className="font-medium text-foreground">{datasetName}</span> is loaded. Ask in plain
            language — the plan, the code and the output all stay on screen.
          </>
        ) : (
          <>
            Drop a file below to begin. {formats.slice(0, 5).join(", ")} and more are read natively;
            nothing is uploaded anywhere.
          </>
        )}
      </p>

      {hasData && (
        <div className="stagger mt-11 grid w-full max-w-2xl gap-2.5 sm:grid-cols-2">
          {OPENERS.map((opener, index) => (
            <button
              key={opener.title}
              type="button"
              onClick={() => onPick(opener.prompt)}
              style={{ "--i": index + 3 } as React.CSSProperties}
              className="lift group rounded-xl border border-border bg-card p-4 text-left shadow-xs hover:border-brand/40"
            >
              <p className="text-[14px] font-medium tracking-[-0.01em]">{opener.title}</p>
              <p className="mt-1.5 font-mono text-[11px] text-muted-foreground transition-colors duration-[var(--duration-base)] group-hover:text-brand">
                {opener.hint}
              </p>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
