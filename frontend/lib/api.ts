/**
 * Backend client.
 *
 * The session id is carried in the `X-Session-Id` header on every call and
 * persisted to localStorage, so a page reload rejoins the same server-side
 * session (and therefore the same loaded dataset and sandbox).
 */

import type {
  ConnectionSummary,
  ConnectionTarget,
  ConnectorKind,
  DataMode,
  DataModeInfo,
  DatasetSummary,
  DocumentSummary,
  ModelDownloadState,
  ModelDownloadsResponse,
  ModelListResponse,
  PermissionProfile,
  PermissionRuling,
  PendingSkill,
  PermissionsInfo,
  ProvidersResponse,
  SandboxSelfTest,
  ServerConfig,
  SessionInfo,
  SkillCandidate,
  SkillDetail,
  SkillDraft,
  SkillInstallPreview,
  SkillListResponse,
  SkillUpdateResult,
  UsageTotals,
  WorkspaceFileEntry,
} from "./types"

export const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_URL?.replace(/\/$/, "") || "http://localhost:8000"

const SESSION_STORAGE_KEY = "wizard.session-id"

export function getStoredSessionId(): string | null {
  if (typeof window === "undefined") return null
  return window.localStorage.getItem(SESSION_STORAGE_KEY)
}

export function storeSessionId(id: string): void {
  if (typeof window === "undefined") return
  window.localStorage.setItem(SESSION_STORAGE_KEY, id)
}

export function clearStoredSessionId(): void {
  if (typeof window === "undefined") return
  window.localStorage.removeItem(SESSION_STORAGE_KEY)
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message)
    this.name = "ApiError"
  }
}

/** Normalises FastAPI's several error shapes into a readable sentence. */
async function extractError(response: Response): Promise<string> {
  try {
    const body = await response.json()
    const detail = body?.detail
    if (typeof detail === "string") return detail
    if (Array.isArray(detail)) {
      return detail
        .map((item) =>
          typeof item === "string" ? item : (item?.msg ?? JSON.stringify(item)),
        )
        .join("; ")
    }
    if (detail) return JSON.stringify(detail)
  } catch {
    // fall through to the status text
  }
  return response.statusText || `Request failed (${response.status})`
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  const sessionId = getStoredSessionId()
  if (sessionId) headers.set("X-Session-Id", sessionId)
  if (init.body && !(init.body instanceof FormData)) {
    headers.set("Content-Type", "application/json")
  }

  const response = await fetch(`${API_BASE_URL}${path}`, { ...init, headers })

  // The server may mint a session on any request; adopt whatever it returns.
  const returned = response.headers.get("X-Session-Id")
  if (returned) storeSessionId(returned)

  if (!response.ok) {
    throw new ApiError(await extractError(response), response.status)
  }
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

export const api = {
  config: () => request<ServerConfig>("/api/config"),

  updateConfig: (payload: import("./types").UpdateConfigPayload) =>
    request<ServerConfig>("/api/config", {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),

  session: () => request<SessionInfo>("/api/session"),

  createSession: () => request<SessionInfo>("/api/session", { method: "POST" }),

  deleteSession: () => request<{ message: string }>("/api/session", { method: "DELETE" }),

  resetNamespace: () => request<SessionInfo>("/api/session/reset", { method: "POST" }),

  models: (refresh = false, provider?: string) => {
    const query = new URLSearchParams()
    if (refresh) query.set("refresh", "true")
    if (provider) query.set("provider", provider)
    const suffix = query.toString()
    return request<ModelListResponse>(`/api/models${suffix ? `?${suffix}` : ""}`)
  },

  selectModels: (selection: Record<string, string | number | null>) =>
    request<SessionInfo>("/api/models", {
      method: "POST",
      body: JSON.stringify(selection),
    }),

  /**
   * What this session will and will not send anywhere.
   *
   * Switching the mode can clear a role's provider assignment — one the new mode
   * forbids — so callers must re-read the session afterwards rather than assume
   * their local copy is still accurate.
   */
  dataMode: () => request<DataModeInfo>("/api/data-mode"),

  setDataMode: (body: { mode?: DataMode; schema_only?: boolean }) =>
    request<DataModeInfo>("/api/data-mode", { method: "POST", body: JSON.stringify(body) }),

  /** Override the session default for one source; delete to follow it again. */
  setDatasetPolicy: (dataset: string, schemaOnly: boolean) =>
    request<DataModeInfo>(`/api/data-mode/dataset/${encodeURIComponent(dataset)}`, {
      method: "PUT",
      body: JSON.stringify({ schema_only: schemaOnly }),
    }),

  clearDatasetPolicy: (dataset: string) =>
    request<DataModeInfo>(`/api/data-mode/dataset/${encodeURIComponent(dataset)}`, {
      method: "DELETE",
    }),

  /**
   * How much this session asks before acting.
   *
   * A separate axis from the data mode: that one decides what is possible at
   * all, this one decides what is asked about among what already is.
   */
  permissions: () => request<PermissionsInfo>("/api/permissions"),

  setPermissions: (body: { profile?: PermissionProfile; categories?: Record<string, PermissionRuling> }) =>
    request<PermissionsInfo>("/api/permissions", { method: "POST", body: JSON.stringify(body) }),

  providers: () => request<ProvidersResponse>("/api/providers"),

  /** The key is written to local disk by the backend and never read back. */
  setProviderKey: (provider: string, apiKey: string) =>
    request<{ status: string; provider: string; key_hint: string }>(
      `/api/providers/${encodeURIComponent(provider)}/credentials`,
      { method: "PUT", body: JSON.stringify({ api_key: apiKey }) },
    ),

  deleteProviderKey: (provider: string) =>
    request<{ status: string; provider: string }>(
      `/api/providers/${encodeURIComponent(provider)}/credentials`,
      { method: "DELETE" },
    ),

  usage: () => request<UsageTotals>("/api/usage"),

  /**
   * Installing a model without leaving the app.
   *
   * Downloads are polled rather than streamed: a pull runs for minutes and
   * survives a reload, so a socket held open for the duration would be the
   * fragile choice.
   */
  modelDownloads: (provider?: string) =>
    request<ModelDownloadsResponse>(
      `/api/models/downloads${provider ? `?provider=${encodeURIComponent(provider)}` : ""}`,
    ),

  downloadModel: (model: string, provider?: string) =>
    request<ModelDownloadState>("/api/models/download", {
      method: "POST",
      body: JSON.stringify({ model, provider: provider ?? null }),
    }),

  cancelModelDownload: (model: string, provider?: string) =>
    request<{ status: string }>("/api/models/download/cancel", {
      method: "POST",
      body: JSON.stringify({ model, provider: provider ?? null }),
    }),

  deleteModel: (model: string, provider?: string) => {
    const query = new URLSearchParams({ model })
    if (provider) query.set("provider", provider)
    return request<{ status: string; model: string }>(
      `/api/models/installed?${query.toString()}`,
      { method: "DELETE" },
    )
  },

  upload: (file: File, clean = true) => {
    const form = new FormData()
    form.append("file", file)
    return request<{
      message: string
      dataset: SessionInfo["datasets"][number]
      cleaning_result: string
      warnings: string[]
      session_id: string
    }>(`/api/datasets?clean=${clean}`, { method: "POST", body: form })
  },

  datasets: () => request<SessionInfo>("/api/datasets"),

  /**
   * Attaches a reference document — a data dictionary, a rules page, a set of
   * metric definitions. These are not data: the agent retrieves from them
   * during a run rather than having them pasted into every prompt.
   */
  uploadDocument: (file: File) => {
    const form = new FormData()
    form.append("file", file)
    return request<{
      message: string
      document: DocumentSummary
      session_id: string
    }>("/api/documents", { method: "POST", body: form })
  },

  deleteDocument: (name: string) =>
    request<{ message: string }>(`/api/documents/${encodeURIComponent(name)}`, {
      method: "DELETE",
    }),

  activateDataset: (name: string) =>
    request<SessionInfo>(`/api/datasets/${encodeURIComponent(name)}/activate`, {
      method: "POST",
    }),

  deleteDataset: (name: string) =>
    request<{ message: string }>(`/api/datasets/${encodeURIComponent(name)}`, {
      method: "DELETE",
    }),

  /**
   * Connections are ingest sources parallel to file upload — a table imported
   * from one lands in `datasets` exactly as an uploaded CSV does. Listing is
   * network-free: it reports what is configured and which drivers are present,
   * and probes nothing, because it renders on every page load.
   */
  connections: () =>
    request<{ connections: ConnectionSummary[]; kinds: ConnectorKind[] }>(
      "/api/connections",
    ),

  createConnection: (body: {
    name: string
    kind: string
    options: Record<string, string>
    secret?: string
  }) =>
    request<ConnectionSummary>("/api/connections", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /**
   * Edits in place rather than delete-and-recreate: recreating would drop the
   * stored secret, every table imported from it, the per-source data policy and
   * the write-back opt-in. Omitting `secret` leaves the stored one alone.
   */
  updateConnection: (
    id: string,
    body: { name: string; kind: string; options: Record<string, string>; secret?: string },
  ) =>
    request<ConnectionSummary>(`/api/connections/${encodeURIComponent(id)}`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),

  deleteConnection: (id: string) =>
    request<{ message: string }>(`/api/connections/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),

  testConnection: (id: string) =>
    request<{ ok: boolean; detail: string }>(
      `/api/connections/${encodeURIComponent(id)}/test`,
      { method: "POST" },
    ),

  /**
   * A POST because it opens a connection to the source. As a GET it slipped past
   * the rate limiter, which only covers mutating methods — and the reason that
   * limiter covers connections at all is that the cost lands on someone else's
   * database.
   */
  connectionSchema: (id: string) =>
    request<{ targets: ConnectionTarget[] }>(
      `/api/connections/${encodeURIComponent(id)}/schema`,
      { method: "POST" },
    ),

  importFromConnection: (id: string, target: string, makeActive = true) =>
    request<{
      message: string
      dataset: DatasetSummary
      truncated: boolean
      session_id: string
    }>(`/api/connections/${encodeURIComponent(id)}/import`, {
      method: "POST",
      body: JSON.stringify({ target, make_active: makeActive }),
    }),

  /**
   * Writes a session table back to the source. Separate from `setWriteBack`:
   * that one says this connection *may* be written to at all, this one is a
   * write. Every session is still asked the first time.
   */
  writeToConnection: (id: string, dataset: string, target: string) =>
    request<{ ok: boolean; detail: string }>(
      `/api/connections/${encodeURIComponent(id)}/write`,
      { method: "POST", body: JSON.stringify({ dataset, target }) },
    ),

  /**
   * Enabling requires the connection's own name typed back. Write-back is the
   * one decision here whose consequences land outside this machine.
   */
  setWriteBack: (id: string, enable: boolean, confirm: string) =>
    request<ConnectionSummary>(
      `/api/connections/${encodeURIComponent(id)}/write-back`,
      { method: "POST", body: JSON.stringify({ enable, confirm }) },
    ),

  preview: (params: {
    page?: number
    perPage?: number
    sortBy?: string | null
    sortOrder?: "asc" | "desc"
    dataset?: string | null
  }) => {
    const query = new URLSearchParams()
    query.set("page", String(params.page ?? 1))
    query.set("per_page", String(params.perPage ?? 50))
    if (params.sortBy) query.set("sort_by", params.sortBy)
    if (params.sortOrder) query.set("sort_order", params.sortOrder)
    if (params.dataset) query.set("dataset", params.dataset)
    return request<{
      page: number
      per_page: number
      total_rows: number
      total_pages: number
      columns: string[]
      data: Record<string, unknown>[]
    }>(`/api/data/preview?${query.toString()}`)
  },

  workspaceFiles: () => request<{ files: WorkspaceFileEntry[] }>("/api/workspace/files"),

  deleteWorkspaceFile: (path: string) =>
    request<{ message: string }>(`/api/workspace/file/${path}`, { method: "DELETE" }),

  variables: () =>
    request<{ variables: Record<string, unknown>; sandbox_available: boolean }>(
      "/api/sandbox/variables",
    ),

  interrupt: () => request<{ status: string }>("/api/sandbox/interrupt", { method: "POST" }),

  /** Spawns a probe that tries to escape. Seconds, not milliseconds — it starts a process. */
  sandboxSelfTest: () => request<SandboxSelfTest>("/api/sandbox/selftest"),

  report: (hours = 24) => request<{ report: string; interaction_count: number }>(`/api/report?hours=${hours}`),

  // ------------------------------------------------------------------ //
  // Skills
  // ------------------------------------------------------------------ //
  skills: () => request<SkillListResponse>("/api/skills"),

  skill: (name: string) => request<SkillDetail>(`/api/skills/${encodeURIComponent(name)}`),

  createSkill: (body: {
    name: string
    description: string
    body: string
    tags?: string[]
    /** Null when this is a hand-written skill or an analysis with nothing
     *  recorded — the backend settles a candidate only when given one. */
    candidate_id?: number | null
  }) => request<SkillDetail>("/api/skills", { method: "POST", body: JSON.stringify(body) }),

  updateSkill: (name: string, body: { description: string; body: string; tags?: string[] }) =>
    request<SkillDetail>(`/api/skills/${encodeURIComponent(name)}`, {
      method: "PUT",
      body: JSON.stringify({ name, ...body }),
    }),

  deleteSkill: (name: string) =>
    request<{ message: string }>(`/api/skills/${encodeURIComponent(name)}`, { method: "DELETE" }),

  /**
   * Re-scans the skill directories. The point of skills being plain files is
   * that a text editor is a valid way to change one, and the backend caches —
   * without this the answer to "I edited the file" would be "restart".
   */
  reloadSkills: () => request<{ message: string; count: number }>("/api/skills/reload", { method: "POST" }),

  skillCandidates: () =>
    request<{ candidates: SkillCandidate[]; threshold: number }>("/api/skills/candidates"),

  /**
   * A draft built from the plan and code that actually ran, so promotion is a
   * confirmation rather than a writing task.
   */
  skillDraft: (id: number) => request<SkillDraft>(`/api/skills/candidates/${id}/draft`),

  /**
   * The other way into promotion: a draft for an analysis the user picked,
   * rather than one that recurred enough for the agent to offer. No threshold —
   * the answer is on screen and they want it saved.
   */
  skillDraftFor: (instruction: string) =>
    request<SkillDraft>("/api/skills/draft", {
      method: "POST",
      body: JSON.stringify({ instruction }),
    }),

  dismissSkillCandidate: (id: number) =>
    request<{ message: string }>(`/api/skills/candidates/${id}/dismiss`, { method: "POST" }),

  // ------------------------------------------------------------------ //
  // Installing from GitHub
  // ------------------------------------------------------------------ //
  /**
   * Fetches a repository or gist, pins it to a commit, and stages it.
   *
   * Installs nothing. What comes back is what the review panel renders: the full
   * body of each skill and the exact commit it came from. Gated by the `network`
   * permission category, so a `deny` ruling returns 403 with its reason.
   */
  previewSkillInstall: (url: string) =>
    request<SkillInstallPreview>("/api/skills/install/preview", {
      method: "POST",
      body: JSON.stringify({ url }),
    }),

  pendingSkills: () => request<{ pending: PendingSkill[]; root: string }>("/api/skills/pending"),

  approvePendingSkill: (id: string) =>
    request<SkillDetail>(`/api/skills/pending/${encodeURIComponent(id)}/approve`, { method: "POST" }),

  discardPendingSkill: (id: string) =>
    request<{ message: string }>(`/api/skills/pending/${encodeURIComponent(id)}`, { method: "DELETE" }),

  /**
   * Re-resolves the pinned ref and reports what changed.
   *
   * `apply` defaults to false: pin-don't-track means an installed skill changes
   * only when someone says so, and applying by default would make the diff a
   * courtesy rather than a step.
   */
  updateSkillFromSource: (name: string, apply = false) =>
    request<SkillUpdateResult>(`/api/skills/${encodeURIComponent(name)}/update`, {
      method: "POST",
      body: JSON.stringify({ apply }),
    }),

  /** Saves or clears the GitHub token. Never read back — only whether one exists. */
  setGitHubToken: (token: string) =>
    request<{ message: string; token_saved: boolean }>("/api/skills/token", {
      method: "POST",
      body: JSON.stringify({ token }),
    }),
}

/** Absolute URL for a file in the current session's workspace. */
export function workspaceFileUrl(path: string, bustCache = false): string {
  const suffix = bustCache ? `?t=${Date.now()}` : ""
  const session = getStoredSessionId()
  const sessionParam = session ? `${bustCache ? "&" : "?"}session=${session}` : ""
  return `${API_BASE_URL}/api/workspace/file/${path}${suffix}${sessionParam}`
}

/**
 * Absolute URL to re-export one finished turn as a script or notebook.
 *
 * A plain `<a href download>` can't set the `X-Session-Id` header, so the
 * session travels as a query param -- same pattern as `workspaceFileUrl` and
 * `websocketUrl`.
 */
export function exportUrl(messageId: number, format: "script" | "notebook"): string {
  const session = getStoredSessionId()
  const sessionParam = session ? `&session=${session}` : ""
  return `${API_BASE_URL}/api/export/${messageId}?format=${format}${sessionParam}`
}

export function websocketUrl(): string {
  const base = API_BASE_URL.replace(/^http/, "ws")
  const session = getStoredSessionId()
  return `${base}/ws/chat${session ? `?session=${session}` : ""}`
}
