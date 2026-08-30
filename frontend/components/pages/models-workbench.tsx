"use client"

import { Check, Cpu, Loader2, RefreshCw, Trash2, TriangleAlert, Zap } from "lucide-react"
import { useCallback, useEffect, useState } from "react"

import { ModelInstall } from "@/components/models/model-install"
import { ProviderKey } from "@/components/models/provider-key"
import { PageHeader } from "@/components/page-header"
import { api } from "@/lib/api"
import type {
  DataModeInfo,
  ModelInfo,
  ModelListResponse,
  ProviderDownloadCapability,
  ProviderId,
  ProviderInfo,
} from "@/lib/types"
import { useSound } from "@/lib/use-sound"
import { cn } from "@/lib/utils"

const ROLES = [
  {
    key: "manager" as const,
    label: "Reasoning",
    blurb: "Reads the schema, plans the analysis and writes the final answer.",
    wants: "reasoning",
  },
  {
    key: "worker" as const,
    label: "Code",
    blurb: "Writes the pandas that actually runs, and repairs it when it fails.",
    wants: "code",
  },
]

/**
 * Labels and hints come from the backend's provider table, not from a map here.
 * There were two copies of that map — this page and the header picker — and both
 * had to be edited by hand whenever a backend was added.
 */
function labelOf(providers: ProviderInfo[], id: string | null): string {
  if (!id) return "—"
  return providers.find((entry) => entry.id === id)?.label ?? id
}

function emptyHint(entry: ProviderInfo | undefined): string {
  if (!entry) return "Pick a provider above."
  if (entry.requires_key && !entry.has_key) {
    return `${entry.label} needs an API key before it can list anything. Add one above.`
  }
  if (!entry.base_url) {
    return `No endpoint is configured for ${entry.label}. Set its base URL in backend/.env.`
  }
  if (entry.id === "ollama") {
    return "Nothing pulled yet, or the daemon is not running. Any model works — try `ollama pull qwen3:8b`."
  }
  if (entry.id === "lmstudio") {
    return "Start the server from LM Studio's Developer tab, and enable “Serve on Local Network” so the backend can reach it from its container."
  }
  return `${entry.label} returned no models. Check the endpoint and the key.`
}

function formatSize(bytes: number): string {
  if (!bytes) return ""
  const gb = bytes / 1024 ** 3
  return gb >= 1 ? `${gb.toFixed(1)} GB` : `${Math.round(bytes / 1024 ** 2)} MB`
}

function formatContext(tokens: number): string {
  if (!tokens) return ""
  return tokens >= 1024 ? `${Math.round(tokens / 1024)}k context` : `${tokens} context`
}

/**
 * The full model surface: every provider, every installed model, and which
 * role each one fills.
 *
 * The header dropdown is for a quick swap mid-conversation. This is where you
 * come to actually see what is available — the dropdown cannot show
 * quantization, context length and load state without becoming a page anyway.
 */
export function ModelsWorkbench() {
  const [data, setData] = useState<ModelListResponse | null>(null)
  const [provider, setProvider] = useState<ProviderId | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState<string | null>(null)
  const [assignError, setAssignError] = useState<string | null>(null)
  const [removing, setRemoving] = useState<string | null>(null)
  const [canDelete, setCanDelete] = useState(false)
  const [dataMode, setDataMode] = useState<DataModeInfo | null>(null)
  const { playSound } = useSound()

  const load = useCallback(async (target?: ProviderId, refresh = false) => {
    setLoading(true)
    try {
      const [response, mode] = await Promise.all([
        api.models(refresh, target),
        api.dataMode().catch(() => null),
      ])
      setData(response)
      setProvider(response.provider as ProviderId)
      if (mode) setDataMode(mode)
    } catch {
      setData(null)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const assignRoles = useCallback(
    async (roles: Array<"manager" | "worker">, model: string) => {
      if (!provider) return
      setSaving(`${roles.join("+")}:${model}`)
      setAssignError(null)
      try {
        const payload: Record<string, string | number | null> = {}
        for (const role of roles) {
          payload[role] = model
          payload[`${role}_provider`] = provider
        }
        await api.selectModels(payload)
        await load(provider)
        playSound("click")
      } catch (caught) {
        // A 409 here is the data mode refusing the provider. Its message names
        // the mode and the role, so it is shown rather than replaced.
        setAssignError(caught instanceof Error ? caught.message : "Could not assign that model.")
      } finally {
        setSaving(null)
      }
    },
    [load, playSound, provider],
  )

  const assign = useCallback(
    (role: "manager" | "worker", model: string) => assignRoles([role], model),
    [assignRoles],
  )
  const assignBoth = useCallback((model: string) => assignRoles(["manager", "worker"], model), [assignRoles])

  const remove = useCallback(
    async (model: string) => {
      if (!provider) return
      if (!window.confirm(`Delete ${model}? It will have to be downloaded again to use it.`)) return
      setRemoving(model)
      try {
        await api.deleteModel(model, provider)
        await load(provider, true)
      } finally {
        setRemoving(null)
      }
    },
    [load, provider],
  )

  /**
   * A finished download has to re-list models, and `refresh` is needed because
   * the registry TTL would otherwise hand back the same list without it.
   */
  const onInstalled = useCallback(() => {
    void load(provider ?? undefined, true)
  }, [load, provider])

  const onCapability = useCallback(
    (capability: ProviderDownloadCapability) => setCanDelete(capability.can_delete),
    [],
  )

  const setTemperature = useCallback(
    async (value: number) => {
      await api.selectModels({ temperature: value })
      setData((current) =>
        current ? { ...current, selected: { ...current.selected, temperature: value } } : current,
      )
    },
    [],
  )

  const selected = data?.selected ?? {}
  const providers = data?.providers ?? []
  const models = data?.models ?? []
  const temperature = typeof selected.temperature === "number" ? selected.temperature : 0
  const activeProvider = providers.find((entry) => entry.id === provider)

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">
      <PageHeader
        eyebrow="Configuration"
        title="Models"
        description="Each role runs on whichever backend you point it at. Reasoning can sit on Ollama while code generation runs in LM Studio — they are resolved separately on every request."
        actions={
          <button
            type="button"
            onClick={() => void load(provider ?? undefined, true)}
            className="flex h-9 items-center gap-2 rounded-lg border border-border bg-card px-3.5 text-[13px] font-medium shadow-xs transition-colors duration-[var(--duration-fast)] hover:border-brand/40"
          >
            <RefreshCw className={cn("h-3.5 w-3.5", loading && "animate-spin")} />
            Rescan
          </button>
        }
      />

      {/* Role assignment ---------------------------------------------------- */}
      <div className="grid gap-3 px-6 py-6 md:grid-cols-2 md:px-9">
        {ROLES.map((role) => {
          const model = String(selected[role.key] ?? "—")
          const roleProvider = String(selected[`${role.key}_provider`] ?? "") as ProviderId
          return (
            <div key={role.key} className="ring-gradient rounded-xl p-4 shadow-sm">
              <div className="flex items-center gap-2">
                <Cpu className="h-3.5 w-3.5 text-brand" />
                <h2 className="text-[13px] font-semibold tracking-[-0.01em]">{role.label}</h2>
              </div>
              <p className="mt-2 text-[12.5px] leading-relaxed text-muted-foreground">{role.blurb}</p>
              <p className="mt-3.5 truncate font-mono text-[13px] font-medium">{model}</p>
              <p className="mt-1 text-[11.5px] text-muted-foreground">
                on {labelOf(providers, roleProvider || null)}
              </p>
            </div>
          )
        })}
      </div>

      {/* Temperature -------------------------------------------------------- */}
      <div className="border-y border-border px-6 py-5 md:px-9">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div>
            <h2 className="flex items-center gap-2 text-[13px] font-semibold tracking-[-0.01em]">
              <Zap className="h-3.5 w-3.5 text-brand" />
              Temperature
            </h2>
            <p className="mt-1.5 max-w-lg text-[12.5px] leading-relaxed text-muted-foreground">
              Applies to every role. Analysis code wants determinism — 0 is the right answer far more
              often than it is for prose.
            </p>
          </div>
          <div className="flex items-center gap-3">
            <input
              type="range"
              min={0}
              max={1}
              step={0.05}
              value={temperature}
              onChange={(event) => void setTemperature(Number(event.target.value))}
              aria-label="Model temperature"
              className="h-1.5 w-44 cursor-pointer appearance-none rounded-full bg-muted accent-[var(--brand)]"
            />
            <span className="tabular w-10 text-right text-[13px] font-medium">
              {temperature.toFixed(2)}
            </span>
          </div>
        </div>
      </div>

      {/* Provider selection ------------------------------------------------- */}
      <div className="px-6 pt-6 md:px-9">
        {assignError && (
          <div className="mb-4 flex items-start gap-2.5 rounded-xl border border-warning/25 bg-warning/8 p-3.5 text-[13px] leading-relaxed text-warning">
            <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0" />
            <span>{assignError}</span>
          </div>
        )}

        <div className="mb-4 flex flex-wrap items-center gap-1.5">
          {providers.map((entry) => (
            <button
              key={entry.id}
              type="button"
              onClick={() => void load(entry.id)}
              // A provider the mode forbids is still browsable — you may want to
              // see what is there before switching. Assigning it is what fails,
              // and the badge below says so before you try.
              title={
                entry.allowed
                  ? entry.base_url || "No endpoint configured"
                  : `${entry.label} is unavailable in ${dataMode?.mode ?? "this"} mode`
              }
              className={cn(
                "flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-[12.5px] font-medium",
                "transition-colors duration-[var(--duration-fast)]",
                provider === entry.id
                  ? "bg-primary text-primary-foreground shadow-xs"
                  : "text-muted-foreground hover:bg-muted hover:text-foreground",
                !entry.configured && provider !== entry.id && "opacity-45",
              )}
            >
              {entry.label}
              {entry.kind === "cloud" && (
                <span
                  className={cn(
                    "rounded px-1 py-0.5 text-[9.5px] uppercase tracking-[0.08em]",
                    provider === entry.id ? "bg-primary-foreground/20" : "bg-muted-foreground/15",
                  )}
                >
                  cloud
                </span>
              )}
            </button>
          ))}
          {activeProvider && (
            <span className="ml-auto truncate font-mono text-[11px] text-muted-foreground">
              {activeProvider.base_url}
            </span>
          )}
        </div>

        {activeProvider && !activeProvider.allowed && (
          <div className="mb-4 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-warning/25 bg-warning/8 p-3.5 text-[12.5px] leading-relaxed text-warning">
            <span>
              {activeProvider.label} cannot be used while this session is set to {dataMode?.mode}.
            </span>
            <button
              type="button"
              onClick={async () => {
                const targetMode = activeProvider.kind === "cloud" ? "cloud-only" : "local-only"
                await api.setDataMode({ mode: targetMode }).catch(() => null)
                await load(provider ?? undefined, true)
              }}
              className="cursor-pointer rounded-lg bg-warning/20 px-3 py-1 text-[12px] font-semibold text-warning transition-colors hover:bg-warning/30"
            >
              Switch to {activeProvider.kind === "cloud" ? "Cloud-Only" : "Local-Only"} Mode
            </button>
          </div>
        )}

        {activeProvider?.hint && (
          <p className="mb-4 text-[12.5px] leading-relaxed text-muted-foreground">{activeProvider.hint}</p>
        )}

        {activeProvider && (
          <div className="mb-5">
            <ProviderKey provider={activeProvider} onChanged={() => void load(activeProvider.id, true)} />
          </div>
        )}
      </div>

      {/* Installing, before browsing: on a fresh machine the list below is
          empty and this is the only control that does anything. */}
      <ModelInstall
        provider={provider}
        installed={models.map((model) => model.name)}
        onInstalled={onInstalled}
        onCapability={onCapability}
      />

      {/* Installed models --------------------------------------------------- */}
      <div className="px-6 py-6 md:px-9">
        {data?.error && (
          <div className="flex items-start gap-2.5 rounded-xl border border-warning/25 bg-warning/8 p-3.5 text-[13px] leading-relaxed text-warning">
            <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0" />
            <span>{data.error}</span>
          </div>
        )}

        {!loading && models.length === 0 && !data?.error && provider && (
          <p className="rounded-xl border border-border bg-card p-5 text-[13px] leading-relaxed text-muted-foreground">
            {emptyHint(activeProvider)}
          </p>
        )}


        {loading && models.length === 0 && (
          <div className="flex justify-center py-12">
            <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
          </div>
        )}

        <div className="grid gap-2.5 lg:grid-cols-2">
          {models.map((model) => (
            <ModelCard
              key={model.name}
              model={model}
              assignedTo={ROLES.filter(
                (role) =>
                  selected[role.key] === model.name && selected[`${role.key}_provider`] === provider,
              ).map((role) => role.label)}
              saving={saving}
              onAssign={assign}
              onAssignBoth={assignBoth}
              onRemove={canDelete ? remove : undefined}
              removing={removing === model.name}
            />
          ))}
        </div>
      </div>
    </div>
  )
}

function ModelCard({
  model,
  assignedTo,
  saving,
  onAssign,
  onAssignBoth,
  onRemove,
  removing,
}: {
  model: ModelInfo
  assignedTo: string[]
  saving: string | null
  onAssign: (role: "manager" | "worker", model: string) => void
  onAssignBoth: (model: string) => void
  /** Undefined when the provider cannot delete — LM Studio's CLI has no verb for it. */
  onRemove?: (model: string) => void
  removing: boolean
}) {
  const facts = [
    model.parameter_size,
    formatSize(model.size_bytes),
    model.quantization,
    formatContext(model.context_length),
  ].filter(Boolean)

  return (
    <article
      className={cn(
        "rounded-xl border bg-card p-4 shadow-xs transition-colors duration-[var(--duration-base)]",
        assignedTo.length > 0 ? "border-brand/45" : "border-border hover:border-brand/25",
      )}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="truncate font-mono text-[13px] font-medium">{model.name}</h3>
          {facts.length > 0 && (
            <p className="mt-1.5 truncate text-[11.5px] text-muted-foreground">{facts.join(" · ")}</p>
          )}
        </div>
        {/* LM Studio loads on first use; that stall is worth warning about. */}
        {model.loaded === false && (
          <span className="shrink-0 rounded-md px-1.5 py-0.5 text-[9.5px] text-muted-foreground ring-1 ring-border">
            not loaded
          </span>
        )}
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-1">
        {model.capabilities.map((capability) => (
          <span
            key={capability}
            className="rounded-md bg-muted px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground"
          >
            {capability}
          </span>
        ))}
      </div>

      <div className="mt-3.5 border-t border-border pt-3">
        {model.capabilities.includes("embedding") ? (
          <p className="text-[11.5px] text-muted-foreground">
            Embedding model — not usable as a chat role.
          </p>
        ) : (
          <>
            {(() => {
              const bothAssigned = assignedTo.length === ROLES.length
              const isSaving = saving === `manager+worker:${model.name}`
              return (
                <button
                  type="button"
                  onClick={() => onAssignBoth(model.name)}
                  disabled={bothAssigned || isSaving}
                  className={cn(
                    "flex w-full items-center justify-center gap-1.5 rounded-lg px-3 py-1.5 text-[12px] font-medium",
                    "transition-colors duration-[var(--duration-fast)]",
                    bothAssigned
                      ? "cursor-default bg-brand-soft text-brand"
                      : "bg-[linear-gradient(120deg,var(--brand),var(--brand-2))] text-brand-foreground shadow-brand hover:brightness-105",
                  )}
                >
                  {isSaving ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  ) : bothAssigned ? (
                    <Check className="h-3.5 w-3.5" />
                  ) : null}
                  {bothAssigned ? "Used for both roles" : "Use for both roles"}
                </button>
              )
            })()}

            <div className="mt-2 flex items-center gap-1.5">
              <span className="text-[10.5px] text-muted-foreground">Assign separately:</span>
              {ROLES.map((role) => {
                const isAssigned = assignedTo.includes(role.label)
                const isSaving = saving === `${role.key}:${model.name}`
                return (
                  <button
                    key={role.key}
                    type="button"
                    onClick={() => onAssign(role.key, model.name)}
                    disabled={isAssigned || isSaving}
                    className={cn(
                      "flex items-center gap-1.5 rounded-md px-2 py-1 text-[11.5px] font-medium",
                      "transition-colors duration-[var(--duration-fast)]",
                      isAssigned
                        ? "cursor-default bg-brand-soft text-brand"
                        : "text-muted-foreground hover:bg-muted hover:text-foreground",
                    )}
                  >
                    {isSaving ? (
                      <Loader2 className="h-3 w-3 animate-spin" />
                    ) : isAssigned ? (
                      <Check className="h-3 w-3" />
                    ) : null}
                    {isAssigned ? role.label : role.label}
                  </button>
                )
              })}

              {onRemove && (
                <button
                  type="button"
                  onClick={() => onRemove(model.name)}
                  disabled={removing}
                  aria-label={`Delete ${model.name}`}
                  title={`Delete ${model.name} from disk`}
                  className="ml-auto rounded-md p-1 text-muted-foreground transition-colors duration-[var(--duration-fast)] hover:bg-warning/10 hover:text-warning disabled:opacity-40"
                >
                  {removing ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  ) : (
                    <Trash2 className="h-3.5 w-3.5" />
                  )}
                </button>
              )}
            </div>
          </>
        )}
      </div>
    </article>
  )
}
