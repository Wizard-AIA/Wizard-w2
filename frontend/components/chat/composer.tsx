"use client"

import { ArrowUp, Loader2, Paperclip, Square, UploadCloud } from "lucide-react"
import { useCallback, useEffect, useRef, useState } from "react"

import { PermissionControl } from "@/components/chat/permission-control"
import type { AnalysisMode } from "@/lib/types"
import { cn } from "@/lib/utils"

interface ComposerProps {
  onSend: (content: string, mode: AnalysisMode) => void
  onStop: () => void
  onUpload: (file: File) => void
  isRunning: boolean
  isUploading: boolean
  hasData: boolean
  disabled?: boolean
  acceptedFormats: string[]
  mode: AnalysisMode
  onModeChange: (mode: AnalysisMode) => void

  onInterrupt: (content: string, mode: AnalysisMode) => void
  restoredDraft?: { text: string; nonce: number } | null
}

const MAX_HEIGHT = 200

/**
 * How much investigation to do.
 *
 * These are depth settings, not workflow settings. The pair they replace —
 * "Plan first" and "Direct" — described whether you would be asked to approve a
 * plan, which is a permissions question and now lives in Settings. What you
 * actually want to control per question is how hard the agent works on it.
 */
const MODES: { key: AnalysisMode; label: string; title: string }[] = [
  {
    key: "auto",
    label: "Auto",
    title: "The agent decides how deep to go. Greetings and quick questions are answered directly.",
  },
  {
    key: "fast",
    label: "Fast",
    title: "No planning and no verification pass. Greetings and quick questions are answered the same way in every depth.",
  },
  {
    key: "deep",
    label: "Deep",
    title: "Always investigates and verifies analysis questions. Greetings and quick questions are still answered directly.",
  },
]

export function Composer({
  onSend,
  onStop,
  onUpload,
  isRunning,
  isUploading,
  hasData,
  disabled,
  acceptedFormats,
  mode,
  onModeChange,
  onInterrupt,
  restoredDraft,
}: ComposerProps) {
  const [value, setValue] = useState("")
  const [dragging, setDragging] = useState(false)
  const [focused, setFocused] = useState(false)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)

  const autoResize = useCallback(() => {
    const node = textareaRef.current
    if (!node) return
    node.style.height = "auto"
    node.style.height = `${Math.min(node.scrollHeight, MAX_HEIGHT)}px`
  }, [])

  useEffect(autoResize, [value, autoResize])

  // A message sent while a turn ran and was refused comes back here. The state is
  // adjusted while rendering (React's pattern for "a prop changed") rather than
  // in an effect, so the text appears in the same paint and nothing cascades.
  // Text the user has typed since is never overwritten.
  const [handledDraft, setHandledDraft] = useState<number | null>(null)
  if (restoredDraft && restoredDraft.nonce !== handledDraft) {
    setHandledDraft(restoredDraft.nonce)
    if (!value.trim()) setValue(restoredDraft.text)
  }
  const draftNonce = restoredDraft?.nonce
  useEffect(() => {
    if (draftNonce !== undefined) textareaRef.current?.focus()
  }, [draftNonce])

  const submit = useCallback(() => {
    const trimmed = value.trim()
    if (!trimmed || disabled) return
    if (isRunning) {
      onInterrupt(trimmed, mode)
    } else {
      onSend(trimmed, mode)
    }
    setValue("")
    requestAnimationFrame(autoResize)
  }, [autoResize, disabled, isRunning, mode, onSend, onInterrupt, value])

  const handleFiles = useCallback(
    (files: FileList | null) => {
      const file = files?.[0]
      if (file) onUpload(file)
    },
    [onUpload],
  )

  const accept = acceptedFormats.map((extension) => `.${extension.replace(/^\./, "")}`).join(",")
  const canSend = Boolean(value.trim()) && !disabled

  return (
    <div className="px-4 pb-5">
      <div
        id="composer"
        // The skip link lands here, so the wrapper takes focus and the textarea
        // is the very next stop rather than the target itself (which would
        // bypass the attach control).
        tabIndex={-1}
        onDragOver={(event) => {
          event.preventDefault()
          setDragging(true)
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => {
          event.preventDefault()
          setDragging(false)
          handleFiles(event.dataTransfer.files)
        }}
        className={cn(
          "relative mx-auto w-full max-w-3xl rounded-2xl border bg-card",
          "transition-[border-color,box-shadow] duration-[var(--duration-base)] ease-[var(--ease-out-quart)]",
          dragging
            ? "border-brand shadow-brand"
            : focused
              ? "border-brand/50 shadow-md"
              : "border-border shadow-sm",
        )}
      >
        {/* Drop target overlay. Covers the composer only while a file is over it. */}
        {dragging && (
          <div className="absolute inset-0 z-10 flex items-center justify-center gap-2.5 rounded-2xl bg-brand-soft/80 backdrop-blur-sm">
            <UploadCloud className="h-4 w-4 text-brand" />
            <span className="text-[13.5px] font-medium text-brand">Drop to load this file</span>
          </div>
        )}

        <div className="flex items-end gap-2 px-3.5 pt-3.5">
          <textarea
            ref={textareaRef}
            value={value}
            onChange={(event) => setValue(event.target.value)}
            onFocus={() => setFocused(true)}
            onBlur={() => setFocused(false)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault()
                submit()
              }
            }}
            rows={1}
            disabled={disabled}
            title={disabled ? "Waiting for your approval above" : undefined}
            placeholder={
              hasData ? "Ask anything about your data…" : "Ask anything, or attach a dataset to analyze…"
            }
            aria-label="Message"
            className="max-h-[200px] flex-1 resize-none bg-transparent py-1 text-[15px] leading-7 placeholder:text-muted-foreground/70 focus:outline-none disabled:opacity-50"
          />

          {isRunning ? (
            <button
              type="button"
              onClick={onStop}
              aria-label="Stop generating"
              className="mb-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-foreground text-background transition-transform duration-[var(--duration-fast)] hover:scale-105 active:scale-95"
            >
              <Square className="h-3 w-3" fill="currentColor" />
            </button>
          ) : (
            <button
              type="button"
              onClick={submit}
              disabled={!canSend}
              aria-label="Send message"
              title={disabled ? "Waiting for your approval above" : "Send message"}
              className={cn(
                "mb-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-xl",
                "transition-all duration-[var(--duration-base)] ease-[var(--ease-out-expo)]",
                canSend
                  ? "bg-[linear-gradient(120deg,var(--brand),var(--brand-2))] text-brand-foreground shadow-brand hover:scale-105 active:scale-95"
                  : "cursor-not-allowed bg-muted text-muted-foreground/60",
              )}
            >
              <ArrowUp className="h-4 w-4" />
            </button>
          )}
        </div>

        <div className="flex items-center justify-between gap-2 px-3.5 pb-3 pt-2">
          <div className="flex items-center gap-1">
            <input
              ref={fileInputRef}
              type="file"
              accept={accept}
              onChange={(event) => {
                handleFiles(event.target.files)
                event.target.value = ""
              }}
              className="hidden"
              aria-label="Upload a dataset"
            />
            <button
              type="button"
              onClick={() => fileInputRef.current?.click()}
              disabled={isUploading || disabled}
              aria-label="Attach a dataset"
              title={`Attach a dataset (${acceptedFormats.slice(0, 6).join(", ")})`}
              className="flex h-7 items-center gap-1.5 rounded-lg px-2 text-[12px] text-muted-foreground transition-colors duration-[var(--duration-fast)] hover:bg-muted hover:text-foreground disabled:opacity-40"
            >
              {isUploading ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Paperclip className="h-3.5 w-3.5" />
              )}
              <span className="hidden sm:inline">{isUploading ? "Reading…" : "Attach"}</span>
            </button>
          </div>

          {/* Two independent dials: how deep, and how much it asks first. */}
          <div className="flex items-center gap-1.5">
            <PermissionControl />
            {/*
              A segmented control rather than a toggle button. The old version
              showed only the *current* mode, so the others — and the fact that
              a choice existed at all — were invisible until you clicked it.
            */}
            <div
              className="flex items-center gap-0.5 rounded-lg bg-muted p-0.5"
              role="radiogroup"
              aria-label="Analysis depth"
              title="Analysis depth. Your choice stays until you change it."
            >
              {MODES.map((option) => (
                <button
                  key={option.key}
                  type="button"
                  role="radio"
                  aria-checked={mode === option.key}
                  onClick={() => onModeChange(option.key)}
                  title={option.title}
                  className={cn(
                    "rounded-md px-2.5 py-1 text-[11.5px] font-medium",
                    "transition-[background-color,color,box-shadow] duration-[var(--duration-fast)]",
                    mode === option.key
                      ? "bg-card text-foreground shadow-xs"
                      : "text-muted-foreground hover:text-foreground",
                  )}
                >
                  {option.label}
                </button>
              ))}
            </div>
          </div>
        </div>
      </div>

      <p className="mx-auto mt-2.5 max-w-3xl text-center text-[11px] text-muted-foreground/80">
        Generated code runs in an isolated sandbox. Check results before you rely on them.
      </p>
    </div>
  )
}
