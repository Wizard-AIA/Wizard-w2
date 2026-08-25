import { tableFromIPC } from "apache-arrow"

import { API_BASE_URL, getStoredSessionId } from "./api"

export type ArrowPreview = {
  columns: string[]
  data: Record<string, unknown>[]
  totalRows: number
}

/** Reads a streamed Arrow IPC response without constructing a JSON payload. */
export async function loadArrowPreview(params: {
  page: number
  perPage: number
  sortBy?: string | null
  sortOrder?: "asc" | "desc"
  dataset?: string | null
}): Promise<ArrowPreview> {
  const query = new URLSearchParams({
    offset: String((params.page - 1) * params.perPage),
    limit: String(params.perPage),
  })
  if (params.sortBy) query.set("sort_by", params.sortBy)
  if (params.sortOrder) query.set("sort_order", params.sortOrder)
  if (params.dataset) query.set("dataset", params.dataset)

  const headers = new Headers()
  const sessionId = getStoredSessionId()
  if (sessionId) headers.set("X-Session-Id", sessionId)

  const response = await fetch(`${API_BASE_URL}/api/workspace/stream-arrow?${query}`, { headers })
  if (!response.ok) {
    throw new Error(response.statusText || `Could not load the Arrow preview (${response.status}).`)
  }

  // Arrow's browser reader accepts the response stream directly. It parses IPC
  // record batches as they arrive and retains columnar buffers until rows are
  // needed by the grid; unlike response.json(), no giant UTF-8 JSON string is
  // created as an intermediate representation.
  const table = await tableFromIPC(response)
  const columns = table.schema.fields.map((field) => field.name)
  const data = table.toArray().map((row) => {
    const record = row as unknown as Record<string, unknown>
    return Object.fromEntries(columns.map((column) => [column, record[column]]))
  })

  return {
    columns,
    data,
    totalRows: Number(response.headers.get("X-Arrow-Total-Rows") ?? data.length),
  }
}
