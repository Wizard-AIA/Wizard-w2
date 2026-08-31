"use client"

import { Check, Copy } from "lucide-react"
import katex from "katex"
import { Fragment, useMemo, useState, type ReactNode } from "react"

import { cn } from "@/lib/utils"

interface MarkdownRendererProps {
  content: string
  className?: string
}

/**
 * Purely presentational markdown renderer.
 *
 * Deliberately stateless with respect to streaming: it re-renders from whatever
 * `content` currently holds. The previous version kept internal "static" and
 * "animating" buffers and applied a per-word blur, which fought with real token
 * streaming (each delta re-triggered the split) and read its own state inside
 * its effect dependency list.
 */
export function MarkdownRenderer({ content, className }: MarkdownRendererProps) {
  const blocks = useMemo(() => parseBlocks(content), [content])

  return (
    <div className={cn("space-y-3 break-words", className)}>
      {blocks.map((block, index) => (
        <Block key={index} block={block} />
      ))}
    </div>
  )
}

type Block =
  | { type: "code"; language: string; content: string }
  | { type: "math"; content: string }
  | { type: "table"; header: string[]; rows: string[][] }
  | { type: "heading"; level: number; content: string }
  | { type: "list"; ordered: boolean; items: string[] }
  | { type: "quote"; content: string }
  | { type: "rule" }
  | { type: "paragraph"; content: string }

/** Splits markdown source into renderable blocks. Tolerates unterminated fences. */
function parseBlocks(source: string): Block[] {
  const lines = source.split("\n")
  const blocks: Block[] = []
  let index = 0

  while (index < lines.length) {
    const line = lines[index]

    if (line.trim().startsWith("```")) {
      const language = line.trim().slice(3).trim()
      const body: string[] = []
      index += 1
      // An unclosed fence is normal mid-stream, so consume to EOF rather than bail.
      while (index < lines.length && !lines[index].trim().startsWith("```")) {
        body.push(lines[index])
        index += 1
      }
      index += 1
      blocks.push({ type: "code", language, content: body.join("\n") })
      continue
    }

    if (line.trim().startsWith("$$") || line.trim().startsWith("\\[")) {
      const isBracket = line.trim().startsWith("\\[")
      const closeMarker = isBracket ? "\\]" : "$$"
      const body: string[] = []
      const firstLine = line.trim().slice(2).trim()
      if (firstLine.endsWith(closeMarker) && firstLine.length >= 2) {
        blocks.push({ type: "math", content: firstLine.slice(0, -closeMarker.length).trim() })
        index += 1
        continue
      }
      if (firstLine) body.push(firstLine)
      index += 1
      while (index < lines.length && !lines[index].trim().endsWith(closeMarker)) {
        body.push(lines[index])
        index += 1
      }
      if (index < lines.length) {
        const lastLine = lines[index].trim().slice(0, -closeMarker.length).trim()
        if (lastLine) body.push(lastLine)
        index += 1
      }
      blocks.push({ type: "math", content: body.join("\n") })
      continue
    }

    if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
      blocks.push({ type: "rule" })
      index += 1
      continue
    }

    const heading = /^(#{1,6})\s+(.*)$/.exec(line)
    if (heading) {
      blocks.push({ type: "heading", level: heading[1].length, content: heading[2] })
      index += 1
      continue
    }

    // A markdown table needs a header row followed by a separator row.
    const nextLine = index + 1 < lines.length ? lines[index + 1] : ""
    if (line.includes("|") && /^[\s|:-]+$/.test(nextLine) && nextLine.includes("-")) {
      const header = splitRow(line)
      index += 2
      const rows: string[][] = []
      while (index < lines.length && lines[index].includes("|") && lines[index].trim()) {
        rows.push(splitRow(lines[index]))
        index += 1
      }
      blocks.push({ type: "table", header, rows })
      continue
    }

    const bullet = /^\s*[-*+]\s+(.*)$/.exec(line)
    const numbered = /^\s*\d+[.)]\s+(.*)$/.exec(line)
    if (bullet || numbered) {
      const ordered = Boolean(numbered)
      const items: string[] = []
      while (index < lines.length) {
        const match = ordered
          ? /^\s*\d+[.)]\s+(.*)$/.exec(lines[index])
          : /^\s*[-*+]\s+(.*)$/.exec(lines[index])
        if (!match) break
        items.push(match[1])
        index += 1
      }
      blocks.push({ type: "list", ordered, items })
      continue
    }

    if (line.trim().startsWith(">")) {
      const quoted: string[] = []
      while (index < lines.length && lines[index].trim().startsWith(">")) {
        quoted.push(lines[index].replace(/^\s*>\s?/, ""))
        index += 1
      }
      blocks.push({ type: "quote", content: quoted.join("\n") })
      continue
    }

    if (!line.trim()) {
      index += 1
      continue
    }

    const paragraph: string[] = []
    while (
      index < lines.length &&
      lines[index].trim() &&
      !isBlockStart(lines[index], index + 1 < lines.length ? lines[index + 1] : undefined)
    ) {
      paragraph.push(lines[index])
      index += 1
    }
    if (paragraph.length === 0) {
      // Defensive: always consume at least one line so the loop terminates.
      paragraph.push(lines[index])
      index += 1
    }
    blocks.push({ type: "paragraph", content: paragraph.join("\n") })
  }

  return blocks
}

function isBlockStart(line: string, nextLine?: string): boolean {
  const trimmed = line.trim()
  return (
    trimmed.startsWith("```") ||
    trimmed.startsWith("$$") ||
    trimmed.startsWith("\\[") ||
    /^#{1,6}\s/.test(trimmed) ||
    /^\s*[-*+]\s/.test(line) ||
    /^\s*\d+[.)]\s/.test(line) ||
    trimmed.startsWith(">") ||
    /^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line) ||
    (line.includes("|") && nextLine !== undefined && /^[\s|:-]+$/.test(nextLine) && nextLine.includes("-"))
  )
}

function splitRow(line: string): string[] {
  return line
    .trim()
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((cell) => cell.trim())
}

function Block({ block }: { block: Block }) {
  switch (block.type) {
    case "code":
      return <CodeBlock language={block.language} content={block.content} />

    case "math":
      return (
        <div className="my-3 overflow-x-auto py-2 text-center">
          <MathSpan math={block.content} display />
        </div>
      )

    case "table":
      return (
        <div className="overflow-x-auto rounded-xl border border-border shadow-xs">
          <table className="w-full text-left text-[12.5px]">
            <thead className="bg-muted">
              <tr>
                {block.header.map((cell, index) => (
                  <th key={index} className="whitespace-nowrap px-3 py-2.5 font-semibold">
                    <Inline text={cell} />
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {block.rows.map((row, rowIndex) => (
                <tr key={rowIndex} className="border-t border-border transition-colors duration-75 hover:bg-accent/40">
                  {row.map((cell, cellIndex) => (
                    <td key={cellIndex} className="tabular whitespace-nowrap px-3 py-2">
                      <Inline text={cell} />
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )

    case "heading": {
      const sizes = ["text-[19px]", "text-[17px]", "text-[15px]", "text-[14px]", "text-[13px]", "text-[12px]"]
      const Tag = `h${Math.min(block.level, 6)}` as "h1"
      return (
        <Tag className={cn("font-semibold tracking-[-0.02em]", sizes[block.level - 1] ?? "text-[14px]")}>
          <Inline text={block.content} />
        </Tag>
      )
    }

    case "list": {
      const Tag = block.ordered ? "ol" : "ul"
      return (
        <Tag
          className={cn(
            "space-y-1.5 pl-5 marker:text-brand/60",
            block.ordered ? "list-decimal" : "list-disc",
          )}
        >
          {block.items.map((item, index) => (
            <li key={index}>
              <Inline text={item} />
            </li>
          ))}
        </Tag>
      )
    }

    case "quote":
      return (
        <blockquote className="border-l-2 border-brand/40 pl-3.5 text-muted-foreground">
          <Inline text={block.content} />
        </blockquote>
      )

    case "rule":
      return <hr className="border-border" />

    default:
      return (
        <div className="whitespace-pre-wrap leading-7">
          <Inline text={block.content} />
        </div>
      )
  }
}

function CodeBlock({ language, content }: { language: string; content: string }) {
  const [copied, setCopied] = useState(false)

  return (
    <div className="overflow-hidden rounded-xl border border-border bg-muted/50 shadow-xs">
      <div className="flex items-center justify-between border-b border-border px-3 py-1.5">
        <span className="font-mono text-[10px] uppercase tracking-[0.14em] text-muted-foreground">
          {language || "code"}
        </span>
        <button
          type="button"
          onClick={() => {
            void navigator.clipboard.writeText(content).then(() => {
              setCopied(true)
              setTimeout(() => setCopied(false), 1600)
            })
          }}
          className="flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] text-muted-foreground transition-colors duration-[var(--duration-fast)] hover:text-foreground"
          aria-label="Copy code"
        >
          {copied ? <Check className="h-3 w-3 text-success" /> : <Copy className="h-3 w-3" />}
        </button>
      </div>
      <pre className="max-h-96 overflow-auto p-3 text-[12px] leading-relaxed">
        <code className="font-mono">{content}</code>
      </pre>
    </div>
  )
}

/** Renders inline emphasis, math formulas, code spans and links. */
function Inline({ text }: { text: string }): ReactNode {
  const pattern = /(\$\$[\s\S]+?\$\$|\\\[[\s\S]+?\\\]|\\\([\s\S]+?\\\)|\$(?!\s)[^$\n]+?(?<!\s)\$|`[^`]+`|\*\*[^*]+\*\*|\*[^*]+\*|_[^_]+_|\[[^\]]+\]\([^)]+\))/g
  const parts = text.split(pattern).filter((part) => part !== undefined && part !== "")

  return (
    <>
      {parts.map((part, index) => {
        if (part.startsWith("$$") && part.endsWith("$$") && part.length >= 4) {
          return <MathSpan key={index} math={part.slice(2, -2)} display />
        }
        if (part.startsWith("\\[") && part.endsWith("\\]") && part.length >= 4) {
          return <MathSpan key={index} math={part.slice(2, -2)} display />
        }
        if (part.startsWith("\\(") && part.endsWith("\\)") && part.length >= 4) {
          return <MathSpan key={index} math={part.slice(2, -2)} />
        }
        if (part.startsWith("$") && part.endsWith("$") && part.length >= 2) {
          return <MathSpan key={index} math={part.slice(1, -1)} />
        }
        if (part.length > 1 && part.startsWith("`") && part.endsWith("`")) {
          return (
            <code
              key={index}
              className="rounded-md border border-border/70 bg-muted px-1.5 py-0.5 font-mono text-[0.85em] text-foreground"
            >
              {part.slice(1, -1)}
            </code>
          )
        }
        if (part.startsWith("**") && part.endsWith("**")) {
          return (
            <strong key={index} className="font-semibold">
              {part.slice(2, -2)}
            </strong>
          )
        }
        if (
          part.length > 2 &&
          ((part.startsWith("*") && part.endsWith("*")) || (part.startsWith("_") && part.endsWith("_")))
        ) {
          return (
            <em key={index} className="italic">
              {part.slice(1, -1)}
            </em>
          )
        }
        const link = /^\[([^\]]+)\]\(([^)]+)\)$/.exec(part)
        if (link) {
          return (
            <a
              key={index}
              href={link[2]}
              target="_blank"
              rel="noopener noreferrer"
              className="text-brand underline decoration-brand/30 underline-offset-2 transition-colors hover:decoration-brand"
            >
              {link[1]}
            </a>
          )
        }
        return <Fragment key={index}>{part}</Fragment>
      })}
    </>
  )
}

function MathSpan({ math, display = false }: { math: string; display?: boolean }) {
  const html = useMemo(() => {
    try {
      // Normalize escaped backslashes before common LaTeX macros (e.g. \\frac -> \frac)
      const normalized = math.trim().replace(/(?<!\\)\\\\(?=[a-zA-Z])/g, "\\")
      return katex.renderToString(normalized, {
        throwOnError: false,
        displayMode: display,
      })
    } catch {
      return math
    }
  }, [math, display])

  return (
    <span
      className={cn(display ? "my-2 block w-full overflow-x-auto text-center py-1" : "inline-block align-baseline")}
      dangerouslySetInnerHTML={{ __html: html }}
    />
  )
}
