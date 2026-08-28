"use client"

import { ArrowDown, ArrowUp, ChevronLeft, ChevronRight, Loader2, Table2 } from "lucide-react"
import { useCallback, useEffect, useState } from "react"

import { loadArrowPreview } from "@/lib/arrow"
import { cn } from "@/lib/utils"

interface DataGridProps {
  /** Name of the dataset to preview; falls back to the session's active one. */
  dataset?: string | null
  perPage?: number
}

function renderCell(value: unknown): string {
  if (value === null || value === undefined) return "—"
  if (typeof value === "number") {
    return Number.isInteger(value) ? value.toLocaleString() : value.toFixed(4).replace(/\.?0+$/, "")
  }
  if (typeof value === "boolean") return value ? "true" : "false"
  return String(value)
}

export function DataGrid({ dataset = null, perPage = 50 }: DataGridProps) {
  const [rows, setRows] = useState<Record<string, unknown>[]>([])
  const [columns, setColumns] = useState<string[]>([])
  const [page, setPage] = useState(1)
  const [totalRows, setTotalRows] = useState(0)
  const [totalPages, setTotalPages] = useState(1)
  const [sortBy, setSortBy] = useState<string | null>(null)
  const [sortOrder, setSortOrder] = useState<"asc" | "desc">("asc")
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const response = await loadArrowPreview({ page, perPage, sortBy, sortOrder, dataset })
      setRows(response.data)
      setColumns(response.columns)
      setTotalRows(response.totalRows)
      setTotalPages(Math.max(1, Math.ceil(response.totalRows / perPage)))
    } catch (exception) {
      setError(exception instanceof Error ? exception.message : "Could not load the preview.")
      setRows([])
      setColumns([])
    } finally {
      setLoading(false)
    }
  }, [dataset, page, perPage, sortBy, sortOrder])

  useEffect(() => {
    void load()
  }, [load])

  // Switching dataset must not leave the viewer on an out-of-range page.
  useEffect(() => {
    setPage(1)
    setSortBy(null)
  }, [dataset])

  const toggleSort = (column: string) => {
    if (sortBy === column) {
      setSortOrder((order) => (order === "asc" ? "desc" : "asc"))
    } else {
      setSortBy(column)
      setSortOrder("asc")
    }
    setPage(1)
  }

  if (error) {
    return (
      <div className="flex h-full items-center justify-center px-6 text-center">
        <p className="text-xs text-muted-foreground">{error}</p>
      </div>
    )
  }

  if (!loading && columns.length === 0) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-1.5 text-center">
        <span className="mb-2 flex h-12 w-12 items-center justify-center rounded-2xl border border-border bg-card shadow-xs">
          <Table2 className="h-5 w-5 text-muted-foreground/50" />
        </span>
        <p className="text-[14px] font-medium">No rows to show</p>
      </div>
    )
  }

  return (
    <div className="flex h-full flex-col">
      <div className="min-h-0 flex-1 overflow-auto">
        <table className="w-full border-collapse text-left text-[12px]">
          <thead className="glass sticky top-0 z-10">
            <tr>
              <th className="w-10 border-b border-border px-2 py-2.5 text-right font-mono text-[10px] font-normal text-muted-foreground">
                #
              </th>
              {columns.map((column) => (
                <th key={column} className="border-b border-border px-3 py-2.5">
                  <button
                    type="button"
                    onClick={() => toggleSort(column)}
                    className="flex items-center gap-1 font-semibold transition-colors duration-[var(--duration-fast)] hover:text-brand"
                  >
                    <span className="max-w-[180px] truncate">{column}</span>
                    {sortBy === column &&
                      (sortOrder === "asc" ? (
                        <ArrowUp className="h-3 w-3 shrink-0" />
                      ) : (
                        <ArrowDown className="h-3 w-3 shrink-0" />
                      ))}
                  </button>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, rowIndex) => (
              <tr key={rowIndex} className="transition-colors duration-75 hover:bg-accent/50">
                <td className="tabular border-b border-border/60 px-2 py-1.5 text-right font-mono text-[10px] text-muted-foreground/70">
                  {(page - 1) * perPage + rowIndex + 1}
                </td>
                {columns.map((column) => (
                  <td
                    key={column}
                    className={cn(
                      "max-w-[240px] truncate border-b border-border/60 px-3 py-1.5",
                      typeof row[column] === "number" && "tabular text-right",
                      row[column] === null && "text-muted-foreground/50",
                    )}
                    title={renderCell(row[column])}
                  >
                    {renderCell(row[column])}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="flex shrink-0 items-center justify-between border-t border-border px-3 py-2">
        <span className="tabular text-[11.5px] text-muted-foreground">
          {loading ? (
            <Loader2 className="h-3 w-3 animate-spin" />
          ) : (
            `${totalRows.toLocaleString()} rows · page ${page} of ${totalPages}`
          )}
        </span>
        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={() => setPage((value) => Math.max(1, value - 1))}
            disabled={page <= 1 || loading}
            aria-label="Previous page"
            className="flex h-7 w-7 items-center justify-center rounded-lg text-muted-foreground transition-colors duration-[var(--duration-fast)] hover:bg-muted hover:text-foreground disabled:opacity-30"
          >
            <ChevronLeft className="h-3.5 w-3.5" />
          </button>
          <button
            type="button"
            onClick={() => setPage((value) => Math.min(totalPages, value + 1))}
            disabled={page >= totalPages || loading}
            aria-label="Next page"
            className="flex h-7 w-7 items-center justify-center rounded-lg text-muted-foreground transition-colors duration-[var(--duration-fast)] hover:bg-muted hover:text-foreground disabled:opacity-30"
          >
            <ChevronRight className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>
    </div>
  )
}
