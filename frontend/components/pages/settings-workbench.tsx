"use client"

import {
  AlertTriangle,
  Check,
  Loader2,
  RotateCcw,
  Save,
  ShieldAlert,
  ShieldCheck,
  Volume2,
  VolumeX,
} from "lucide-react"
import { useCallback, useEffect, useState } from "react"

import { PageHeader, Section } from "@/components/page-header"
import { api, clearStoredSessionId, getStoredSessionId } from "@/lib/api"
import type {
  ExecutionBackend,
  PermissionProfile,
  PermissionRuling,
  PermissionsInfo,
  SandboxSelfTest,
  ServerConfig,
  SessionInfo,
  UpdateConfigPayload,
  UsageTotals,
} from "@/lib/types"
import { useSound } from "@/lib/use-sound"
import { cn } from "@/lib/utils"
import { useWorkspace } from "@/lib/workspace-context"

export function SettingsWorkbench() {
  const { config: globalConfig, refreshConfig: globalRefreshConfig } = useWorkspace()
  const [config, setConfig] = useState<ServerConfig | null>(globalConfig)
  const [session, setSession] = useState<SessionInfo | null>(null)
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [permissions, setPermissions] = useState<PermissionsInfo | null>(null)
  const [usage, setUsage] = useState<UsageTotals | null>(null)
  const { soundOn, toggleSound, playSound } = useSound()

  // Form State for Mutable Settings
  const [form, setForm] = useState<UpdateConfigPayload>({})
  const [savedSuccess, setSavedSuccess] = useState(false)
  const [savingError, setSavingError] = useState<string | null>(null)

  const syncFormFromConfig = useCallback((cfg: ServerConfig) => {
    setForm({
      api_provider: cfg.api_provider || cfg.model_provider || "ollama",
      data_mode: (cfg.data_mode || "local-only") as import("@/lib/types").DataMode,
      data_schema_only: cfg.data_schema_only ?? true,
      execution_backend: cfg.execution_backend,
      host_sandbox: cfg.host_sandbox as "off" | "best-effort" | "require",
      host_sandbox_network: (cfg.host_sandbox_network || "deny") as "deny" | "allow",
      sandbox_tier: (cfg.sandbox_tier || "standard") as "core" | "standard" | "full",
      sandbox_mem_limit: cfg.sandbox_mem_limit || "2g",
      sandbox_exec_timeout: cfg.sandbox_exec_timeout ?? 180,
      max_upload_mb: cfg.max_upload_mb || 512,
      plot_format: cfg.plot_format || "html",
      agent_tier: (cfg.agent_tier || "auto") as "auto" | "compact" | "balanced" | "full",
      agent_max_iterations: cfg.agent_max_iterations ?? 24,
      agent_turn_timeout: cfg.agent_turn_timeout ?? 300,
      agent_require_approval: cfg.agent_require_approval ?? false,
      agent_verify: cfg.agent_verify ?? true,
      agent_grounding_check: cfg.agent_grounding_check ?? true,
      agent_emit_script: cfg.agent_emit_script ?? true,
      subagent_enabled: cfg.subagent_enabled ?? true,
      subagent_max_iterations: cfg.subagent_max_iterations ?? 3,
      council_enabled: cfg.council_enabled ?? true,
      vision_enabled: cfg.vision_enabled ?? false,
      context_docs_enabled: cfg.context_docs_enabled ?? true,
      skills_enabled: cfg.skills_enabled ?? true,
      temperature: cfg.temperature ?? 0.0,
      max_tokens: cfg.max_tokens ?? 4096,
      llm_num_thread: cfg.llm_num_thread ?? 0,
      llm_num_ctx: cfg.llm_num_ctx ?? 0,
      llm_keep_alive: cfg.llm_keep_alive || "30m",
      rag_enabled: cfg.rag_enabled ?? false,
      ollama_base_url: cfg.ollama_base_url || "http://localhost:11434",
      lmstudio_base_url: cfg.lmstudio_base_url || "http://localhost:1234",
      openai_base_url: cfg.openai_base_url || "https://api.openai.com/v1",
      openai_api_key: "",
      anthropic_base_url: cfg.anthropic_base_url || "https://api.anthropic.com",
      anthropic_api_key: "",
      gemini_base_url: cfg.gemini_base_url || "https://generativelanguage.googleapis.com/v1beta",
      gemini_api_key: "",
      gateway_api_url: cfg.gateway_api_url || "",
      gateway_api_key: "",
    })
  }, [])

  const refresh = useCallback(async () => {
    const [nextConfig, nextSession, nextPermissions, nextUsage] = await Promise.allSettled([
      api.config(),
      api.session(),
      api.permissions(),
      api.usage(),
    ])
    if (nextConfig.status === "fulfilled") {
      setConfig(nextConfig.value)
      syncFormFromConfig(nextConfig.value)
    }
    if (nextSession.status === "fulfilled") setSession(nextSession.value)
    if (nextPermissions.status === "fulfilled") setPermissions(nextPermissions.value)
    if (nextUsage.status === "fulfilled") setUsage(nextUsage.value)
    setSessionId(getStoredSessionId())
  }, [syncFormFromConfig])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const handleSaveSettings = useCallback(async () => {
    setBusy("save-config")
    setSavingError(null)
    setSavedSuccess(false)
    try {
      // Filter out empty API keys so we don't wipe existing stored keys unless requested
      const payload: UpdateConfigPayload = { ...form }
      if (!payload.openai_api_key) delete payload.openai_api_key
      if (!payload.anthropic_api_key) delete payload.anthropic_api_key
      if (!payload.gemini_api_key) delete payload.gemini_api_key
      if (!payload.gateway_api_key) delete payload.gateway_api_key

      const updated = await api.updateConfig(payload)
      setConfig(updated)
      syncFormFromConfig(updated)
      await globalRefreshConfig()
      setSavedSuccess(true)
      playSound("click")
      setTimeout(() => setSavedSuccess(false), 4000)
    } catch (err) {
      setSavingError(err instanceof Error ? err.message : "Failed to update configuration")
    } finally {
      setBusy(null)
    }
  }, [form, globalRefreshConfig, playSound, syncFormFromConfig])

  const setProfile = useCallback(async (profile: PermissionProfile) => {
    setPermissions(await api.setPermissions({ profile }))
  }, [])

  const setRuling = useCallback(async (key: string, ruling: PermissionRuling) => {
    setPermissions(await api.setPermissions({ categories: { [key]: ruling } }))
  }, [])

  const resetWorkspace = useCallback(async () => {
    setBusy("reset")
    try {
      await api.resetNamespace()
      await refresh()
    } finally {
      setBusy(null)
    }
  }, [refresh])

  const newSession = useCallback(async () => {
    setBusy("session")
    try {
      await api.deleteSession()
    } catch {
      // Already gone
    } finally {
      clearStoredSessionId()
      await refresh()
      setBusy(null)
    }
  }, [refresh])

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-y-auto pb-16">
      <PageHeader
        eyebrow="Preferences"
        title="Settings"
        description="Configure runtime execution, agent depth tiers, safety verification, LLM inference, and API keys."
        actions={
          <button
            type="button"
            onClick={() => void handleSaveSettings()}
            disabled={busy === "save-config"}
            className="flex items-center gap-2 rounded-lg bg-[linear-gradient(120deg,var(--brand),var(--brand-2))] px-4 py-2 text-[13px] font-medium text-brand-foreground shadow-brand transition-all hover:brightness-105 active:scale-[0.985] disabled:opacity-50"
          >
            {busy === "save-config" ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : savedSuccess ? (
              <Check className="h-4 w-4 text-white" />
            ) : (
              <Save className="h-4 w-4" />
            )}
            {busy === "save-config" ? "Saving..." : savedSuccess ? "Settings Saved!" : "Save Settings"}
          </button>
        }
      />

      {savingError && (
        <div className="mx-6 mt-4 flex items-start gap-2.5 rounded-xl border border-destructive/25 bg-destructive/8 p-3.5 text-sm text-destructive md:mx-9">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <span>{savingError}</span>
        </div>
      )}

      {savedSuccess && (
        <div className="mx-6 mt-4 flex items-start gap-2.5 rounded-xl border border-success/30 bg-success/10 p-3.5 text-sm text-success md:mx-9">
          <Check className="mt-0.5 h-4 w-4 shrink-0" />
          <span>Runtime configuration updated and persisted to environment successfully.</span>
        </div>
      )}

      {/* 1. Global Environment & Provider Defaults */}
      <Section
        title="Environment & Provider Defaults"
        description="Global runtime backend and privacy settings saved to backend/.env."
      >
        <div className="space-y-4 rounded-xl border border-border bg-card p-5 shadow-xs">
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {/* Default API Provider */}
            <div>
              <label className="block text-[12px] font-semibold uppercase tracking-wider text-muted-foreground mb-1.5">
                Default LLM Provider
              </label>
              <select
                value={form.api_provider ?? "ollama"}
                onChange={(e) => setForm({ ...form, api_provider: e.target.value })}
                className="w-full rounded-lg border border-border bg-background px-3 py-2 text-[13px] font-medium text-foreground focus:border-brand focus:outline-none"
              >
                <option value="ollama">Ollama (Local / On-Device)</option>
                <option value="lmstudio">LM Studio (Local / On-Device)</option>
                <option value="gemini">Google Gemini (Cloud)</option>
                <option value="openai">OpenAI (Cloud)</option>
                <option value="anthropic">Anthropic (Cloud)</option>
                <option value="custom_gateway">Custom Gateway (Groq, OpenRouter, vLLM)</option>
              </select>
              <p className="mt-1 text-[11px] text-muted-foreground">
                The primary inference engine for new sessions.
              </p>
            </div>

            {/* Default Data Mode */}
            <div>
              <label className="block text-[12px] font-semibold uppercase tracking-wider text-muted-foreground mb-1.5">
                Data Mode & Privacy Tier
              </label>
              <select
                value={form.data_mode ?? "local-only"}
                onChange={(e) => setForm({ ...form, data_mode: e.target.value as import("@/lib/types").DataMode })}
                className="w-full rounded-lg border border-border bg-background px-3 py-2 text-[13px] font-medium text-foreground focus:border-brand focus:outline-none"
              >
                <option value="local-only">Local Only (100% on-device)</option>
                <option value="cloud-only">Cloud Only (Cloud frontier models)</option>
                <option value="hybrid">Hybrid (Local worker + Cloud planner)</option>
              </select>
              <p className="mt-1 text-[11px] text-muted-foreground">
                Enforces whether data may leave this machine.
              </p>
            </div>

            {/* Schema Only Mode */}
            <div className="flex flex-col justify-end">
              <label className="flex items-center justify-between p-3 rounded-lg border border-border bg-muted/20 hover:bg-muted/40 cursor-pointer transition-colors">
                <div>
                  <span className="block text-[13px] font-medium text-foreground">Schema-Only Mode</span>
                  <span className="block text-[11.5px] text-muted-foreground">Withhold raw data rows from cloud prompts</span>
                </div>
                <input
                  type="checkbox"
                  checked={form.data_schema_only ?? true}
                  onChange={(e) => setForm({ ...form, data_schema_only: e.target.checked })}
                  className="h-4 w-4 rounded text-brand focus:ring-brand"
                />
              </label>
            </div>
          </div>
        </div>
      </Section>

      {/* 2. Interface Preferences */}
      <Section
        title="Interface"
        description="Stored in this browser only. Nothing here is sent to the server."
      >
        <div className="flex items-center justify-between gap-4 rounded-xl border border-border bg-card p-4 shadow-xs">
          <div className="flex min-w-0 items-start gap-3">
            {soundOn ? (
              <Volume2 className="mt-0.5 h-4 w-4 shrink-0 text-brand" />
            ) : (
              <VolumeX className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
            )}
            <div className="min-w-0">
              <p className="text-[13.5px] font-medium">Interface sounds</p>
              <p className="mt-1 text-[12.5px] leading-relaxed text-muted-foreground">
                A short chime on load and a click when you send. Off is remembered.
              </p>
            </div>
          </div>
          <button
            type="button"
            role="switch"
            aria-checked={soundOn}
            aria-label="Interface sounds"
            onClick={toggleSound}
            className={cn(
              "relative h-6 w-11 shrink-0 rounded-full transition-colors duration-[var(--duration-base)]",
              soundOn ? "bg-brand" : "bg-muted",
            )}
          >
            <span
              className={cn(
                "block h-5 w-5 rounded-full bg-white shadow-xs transition-transform duration-[var(--duration-base)]",
                soundOn ? "translate-x-5.5" : "translate-x-0.5",
              )}
            />
          </button>
        </div>
      </Section>

      {/* 3. Execution & Sandboxing Controls */}
      <Section
        title="Execution & Sandbox"
        description="Where generated Python runs and how tightly it is isolated from your filesystem and network."
      >
        <div className="space-y-4 rounded-xl border border-border bg-card p-5 shadow-xs">
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {/* Execution Backend */}
            <div>
              <label className="block text-[12px] font-semibold uppercase tracking-wider text-muted-foreground mb-1.5">
                Execution Backend
              </label>
              <select
                value={form.execution_backend ?? "host"}
                onChange={(e) => setForm({ ...form, execution_backend: e.target.value as ExecutionBackend })}
                className="w-full rounded-lg border border-border bg-background px-3 py-2 text-[13px] font-medium text-foreground focus:border-brand focus:outline-none"
              >
                <option value="host">Host Subprocess (Recommended)</option>
                <option value="docker">Docker Container (Highest Isolation)</option>
                <option value="inprocess">In-Process (CI / Fast Testing Only)</option>
              </select>
              <p className="mt-1 text-[11px] text-muted-foreground">
                Where python commands and analytical scripts execute.
              </p>
            </div>

            {/* Host Sandbox Mode */}
            <div>
              <label className="block text-[12px] font-semibold uppercase tracking-wider text-muted-foreground mb-1.5">
                Host OS Containment
              </label>
              <select
                value={form.host_sandbox ?? "best-effort"}
                onChange={(e) => setForm({ ...form, host_sandbox: e.target.value as "off" | "best-effort" | "require" })}
                className="w-full rounded-lg border border-border bg-background px-3 py-2 text-[13px] font-medium text-foreground focus:border-brand focus:outline-none"
              >
                <option value="best-effort">Best Effort (Applies OS Sandbox)</option>
                <option value="require">Require (Refuses if unsupported)</option>
                <option value="off">Off (Standard Subprocess)</option>
              </select>
              <p className="mt-1 text-[11px] text-muted-foreground">
                Uses Seatbelt (macOS) or Landlock/cgroups (Linux).
              </p>
            </div>

            {/* Sandbox Network */}
            <div>
              <label className="block text-[12px] font-semibold uppercase tracking-wider text-muted-foreground mb-1.5">
                Outbound Network Policy
              </label>
              <select
                value={form.host_sandbox_network ?? "deny"}
                onChange={(e) => setForm({ ...form, host_sandbox_network: e.target.value as "deny" | "allow" })}
                className="w-full rounded-lg border border-border bg-background px-3 py-2 text-[13px] font-medium text-foreground focus:border-brand focus:outline-none"
              >
                <option value="deny">Deny (Block All Outbound Traffic)</option>
                <option value="allow">Allow (Allow Outbound Network)</option>
              </select>
              <p className="mt-1 text-[11px] text-muted-foreground">
                Prevents data leakage or unauthorized external requests.
              </p>
            </div>

            {/* Toolkit Tier */}
            <div>
              <label className="block text-[12px] font-semibold uppercase tracking-wider text-muted-foreground mb-1.5">
                Toolkit Tier
              </label>
              <select
                value={form.sandbox_tier ?? "standard"}
                onChange={(e) => setForm({ ...form, sandbox_tier: e.target.value as "core" | "standard" | "full" })}
                className="w-full rounded-lg border border-border bg-background px-3 py-2 text-[13px] font-medium text-foreground focus:border-brand focus:outline-none"
              >
                <option value="core">Core (Pandas, DuckDB, Plotly)</option>
                <option value="standard">Standard (+ Scikit-learn, Scipy, Statsmodels)</option>
                <option value="full">Full (+ Geospatial, PyTorch, Lifelines)</option>
              </select>
              <p className="mt-1 text-[11px] text-muted-foreground">
                Preloaded library environment inside the sandbox.
              </p>
            </div>

            {/* Memory Limit */}
            <div>
              <label className="block text-[12px] font-semibold uppercase tracking-wider text-muted-foreground mb-1.5">
                Sandbox Memory Limit
              </label>
              <input
                type="text"
                value={form.sandbox_mem_limit ?? "2g"}
                onChange={(e) => setForm({ ...form, sandbox_mem_limit: e.target.value })}
                placeholder="2g, 4g, 8g"
                className="w-full rounded-lg border border-border bg-background px-3 py-2 text-[13px] font-mono text-foreground focus:border-brand focus:outline-none"
              />
              <p className="mt-1 text-[11px] text-muted-foreground">
                Hard ceiling for analytical memory allocations.
              </p>
            </div>

            {/* Plot Format */}
            <div>
              <label className="block text-[12px] font-semibold uppercase tracking-wider text-muted-foreground mb-1.5">
                Default Plot Format
              </label>
              <select
                value={form.plot_format ?? "html"}
                onChange={(e) => setForm({ ...form, plot_format: e.target.value as "html" | "png" })}
                className="w-full rounded-lg border border-border bg-background px-3 py-2 text-[13px] font-medium text-foreground focus:border-brand focus:outline-none"
              >
                <option value="html">Interactive HTML (Plotly / Vega)</option>
                <option value="png">Static PNG (Matplotlib / Seaborn)</option>
              </select>
              <p className="mt-1 text-[11px] text-muted-foreground">
                Generated visualization rendering artifact format.
              </p>
            </div>

            {/* Execution Timeout */}
            <div>
              <label className="block text-[12px] font-semibold uppercase tracking-wider text-muted-foreground mb-1.5">
                Execution Timeout (Seconds)
              </label>
              <input
                type="number"
                min={10}
                max={3600}
                value={form.sandbox_exec_timeout ?? 180}
                onChange={(e) => setForm({ ...form, sandbox_exec_timeout: parseInt(e.target.value, 10) || 180 })}
                className="w-full rounded-lg border border-border bg-background px-3 py-2 text-[13px] font-mono text-foreground focus:border-brand focus:outline-none"
              />
              <p className="mt-1 text-[11px] text-muted-foreground">
                Per-step script execution deadline before termination.
              </p>
            </div>
          </div>

          {config && <IsolationNote isolation={config.execution_isolation} />}
          {config && <SandboxPanel config={config} />}
        </div>
      </Section>

      {/* 4. Agent Reasoning & Verification Controls */}
      <Section
        title="Agent Reasoning & Safety Loop"
        description="Control iteration budgets, multi-turn verification, grounding checks, and approval gates."
      >
        <div className="space-y-4 rounded-xl border border-border bg-card p-5 shadow-xs">
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            {/* Depth Tier */}
            <div>
              <label className="block text-[12px] font-semibold uppercase tracking-wider text-muted-foreground mb-1.5">
                Depth Tier
              </label>
              <select
                value={form.agent_tier ?? "auto"}
                onChange={(e) => setForm({ ...form, agent_tier: e.target.value as "auto" | "compact" | "balanced" | "full" })}
                className="w-full rounded-lg border border-border bg-background px-3 py-2 text-[13px] font-medium text-foreground focus:border-brand focus:outline-none"
              >
                <option value="auto">Auto (Adaptive based on model size)</option>
                <option value="compact">Compact (Fast, lightweight turns)</option>
                <option value="balanced">Balanced (Standard reasoning & checks)</option>
                <option value="full">Full (Deep exploratory & competing hypotheses)</option>
              </select>
              <p className="mt-1 text-[11px] text-muted-foreground">
                Determines the depth of planning and step breakdown.
              </p>
            </div>

            {/* Max Iterations */}
            <div>
              <label className="block text-[12px] font-semibold uppercase tracking-wider text-muted-foreground mb-1.5">
                Max Turn Budget
              </label>
              <input
                type="number"
                min={1}
                max={100}
                value={form.agent_max_iterations ?? 24}
                onChange={(e) => setForm({ ...form, agent_max_iterations: parseInt(e.target.value, 10) || 24 })}
                className="w-full rounded-lg border border-border bg-background px-3 py-2 text-[13px] font-mono text-foreground focus:border-brand focus:outline-none"
              />
              <p className="mt-1 text-[11px] text-muted-foreground">
                Maximum tool steps before forcing conclusion.
              </p>
            </div>

            {/* Subagent Max Iterations */}
            <div>
              <label className="block text-[12px] font-semibold uppercase tracking-wider text-muted-foreground mb-1.5">
                Subagent Budget
              </label>
              <input
                type="number"
                min={1}
                max={20}
                value={form.subagent_max_iterations ?? 3}
                onChange={(e) => setForm({ ...form, subagent_max_iterations: parseInt(e.target.value, 10) || 3 })}
                className="w-full rounded-lg border border-border bg-background px-3 py-2 text-[13px] font-mono text-foreground focus:border-brand focus:outline-none"
              />
              <p className="mt-1 text-[11px] text-muted-foreground">
                Maximum tool steps per subagent branch.
              </p>
            </div>

            {/* Turn Timeout */}
            <div>
              <label className="block text-[12px] font-semibold uppercase tracking-wider text-muted-foreground mb-1.5">
                Turn Deadline (s)
              </label>
              <input
                type="number"
                min={10}
                max={1800}
                value={form.agent_turn_timeout ?? 300}
                onChange={(e) => setForm({ ...form, agent_turn_timeout: parseFloat(e.target.value) || 300 })}
                className="w-full rounded-lg border border-border bg-background px-3 py-2 text-[13px] font-mono text-foreground focus:border-brand focus:outline-none"
              />
              <p className="mt-1 text-[11px] text-muted-foreground">
                Overall timeout deadline per question.
              </p>
            </div>
          </div>

          <div className="mt-4 grid gap-3 pt-3 border-t border-border sm:grid-cols-2">
            {/* Approval Gate */}
            <label className="flex items-center justify-between p-3 rounded-lg border border-border bg-muted/20 hover:bg-muted/40 cursor-pointer transition-colors">
              <div>
                <span className="block text-[13px] font-medium text-foreground">Require Plan Approval</span>
                <span className="block text-[11.5px] text-muted-foreground">Pause for human consent before executing analysis plan</span>
              </div>
              <input
                type="checkbox"
                checked={form.agent_require_approval ?? false}
                onChange={(e) => setForm({ ...form, agent_require_approval: e.target.checked })}
                className="h-4 w-4 rounded text-brand focus:ring-brand"
              />
            </label>

            {/* Verification Recompute */}
            <label className="flex items-center justify-between p-3 rounded-lg border border-border bg-muted/20 hover:bg-muted/40 cursor-pointer transition-colors">
              <div>
                <span className="block text-[13px] font-medium text-foreground">Independent Verification Pass</span>
                <span className="block text-[11.5px] text-muted-foreground">Re-derives findings with alternative code to ensure integrity</span>
              </div>
              <input
                type="checkbox"
                checked={form.agent_verify ?? true}
                onChange={(e) => setForm({ ...form, agent_verify: e.target.checked })}
                className="h-4 w-4 rounded text-brand focus:ring-brand"
              />
            </label>

            {/* Grounding Check */}
            <label className="flex items-center justify-between p-3 rounded-lg border border-border bg-muted/20 hover:bg-muted/40 cursor-pointer transition-colors">
              <div>
                <span className="block text-[13px] font-medium text-foreground">Strict Grounding Verification</span>
                <span className="block text-[11.5px] text-muted-foreground">Checks that every claimed fact matches real execution output</span>
              </div>
              <input
                type="checkbox"
                checked={form.agent_grounding_check ?? true}
                onChange={(e) => setForm({ ...form, agent_grounding_check: e.target.checked })}
                className="h-4 w-4 rounded text-brand focus:ring-brand"
              />
            </label>

            {/* Emit Python Script */}
            <label className="flex items-center justify-between p-3 rounded-lg border border-border bg-muted/20 hover:bg-muted/40 cursor-pointer transition-colors">
              <div>
                <span className="block text-[13px] font-medium text-foreground">Emit Reproducible Script</span>
                <span className="block text-[11.5px] text-muted-foreground">Generates a self-contained clean script artifact at the end</span>
              </div>
              <input
                type="checkbox"
                checked={form.agent_emit_script ?? true}
                onChange={(e) => setForm({ ...form, agent_emit_script: e.target.checked })}
                className="h-4 w-4 rounded text-brand focus:ring-brand"
              />
            </label>

            {/* Subagent Enabled */}
            <label className="flex items-center justify-between p-3 rounded-lg border border-border bg-muted/20 hover:bg-muted/40 cursor-pointer transition-colors">
              <div>
                <span className="block text-[13px] font-medium text-foreground">Subagent Forking</span>
                <span className="block text-[11.5px] text-muted-foreground">Allow spawning parallel subagents for competing hypotheses</span>
              </div>
              <input
                type="checkbox"
                checked={form.subagent_enabled ?? true}
                onChange={(e) => setForm({ ...form, subagent_enabled: e.target.checked })}
                className="h-4 w-4 rounded text-brand focus:ring-brand"
              />
            </label>

            {/* Council Review */}
            <label className="flex items-center justify-between p-3 rounded-lg border border-border bg-muted/20 hover:bg-muted/40 cursor-pointer transition-colors">
              <div>
                <span className="block text-[13px] font-medium text-foreground">Council of Specialists</span>
                <span className="block text-[11.5px] text-muted-foreground">Run specialist peer reviews before finalizing findings</span>
              </div>
              <input
                type="checkbox"
                checked={form.council_enabled ?? true}
                onChange={(e) => setForm({ ...form, council_enabled: e.target.checked })}
                className="h-4 w-4 rounded text-brand focus:ring-brand"
              />
            </label>

            {/* Vision Chart Analysis */}
            <label className="flex items-center justify-between p-3 rounded-lg border border-border bg-muted/20 hover:bg-muted/40 cursor-pointer transition-colors">
              <div>
                <span className="block text-[13px] font-medium text-foreground">Vision Chart Analysis</span>
                <span className="block text-[11.5px] text-muted-foreground">Inspect generated charts visually via multimodal models</span>
              </div>
              <input
                type="checkbox"
                checked={form.vision_enabled ?? false}
                onChange={(e) => setForm({ ...form, vision_enabled: e.target.checked })}
                className="h-4 w-4 rounded text-brand focus:ring-brand"
              />
            </label>

            {/* Context Documents */}
            <label className="flex items-center justify-between p-3 rounded-lg border border-border bg-muted/20 hover:bg-muted/40 cursor-pointer transition-colors">
              <div>
                <span className="block text-[13px] font-medium text-foreground">Context Documents (PDF / DOCX)</span>
                <span className="block text-[11.5px] text-muted-foreground">Attach reference guides, dictionaries, and domain rules</span>
              </div>
              <input
                type="checkbox"
                checked={form.context_docs_enabled ?? true}
                onChange={(e) => setForm({ ...form, context_docs_enabled: e.target.checked })}
                className="h-4 w-4 rounded text-brand focus:ring-brand"
              />
            </label>

            {/* RAG Enabled */}
            <label className="flex items-center justify-between p-3 rounded-lg border border-border bg-muted/20 hover:bg-muted/40 cursor-pointer transition-colors">
              <div>
                <span className="block text-[13px] font-medium text-foreground">Semantic Document Retrieval (RAG)</span>
                <span className="block text-[11.5px] text-muted-foreground">Retrieve relevant chunks from attached PDF / DOCX files</span>
              </div>
              <input
                type="checkbox"
                checked={form.rag_enabled ?? false}
                onChange={(e) => setForm({ ...form, rag_enabled: e.target.checked })}
                className="h-4 w-4 rounded text-brand focus:ring-brand"
              />
            </label>

            {/* Skills System */}
            <label className="flex items-center justify-between p-3 rounded-lg border border-border bg-muted/20 hover:bg-muted/40 cursor-pointer transition-colors">
              <div>
                <span className="block text-[13px] font-medium text-foreground">Skills Engine & Promotion</span>
                <span className="block text-[11.5px] text-muted-foreground">Enable reusable analytical skill recipes and auto-suggestions</span>
              </div>
              <input
                type="checkbox"
                checked={form.skills_enabled ?? true}
                onChange={(e) => setForm({ ...form, skills_enabled: e.target.checked })}
                className="h-4 w-4 rounded text-brand focus:ring-brand"
              />
            </label>
          </div>
        </div>
      </Section>

      {/* 5. LLM Inference & Local Host Parameters */}
      <Section
        title="LLM Inference Parameters"
        description="Tune generation hyperparameters, context window sizes, thread pools, and cache keep-alive."
      >
        <div className="space-y-4 rounded-xl border border-border bg-card p-5 shadow-xs">
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {/* Temperature */}
            <div>
              <div className="flex items-center justify-between mb-1.5">
                <label className="text-[12px] font-semibold uppercase tracking-wider text-muted-foreground">
                  Temperature
                </label>
                <span className="font-mono text-[12px] text-foreground font-medium">{form.temperature?.toFixed(2) ?? "0.00"}</span>
              </div>
              <input
                type="range"
                min={0}
                max={1}
                step={0.05}
                value={form.temperature ?? 0}
                onChange={(e) => setForm({ ...form, temperature: parseFloat(e.target.value) })}
                className="w-full accent-brand"
              />
              <p className="mt-1 text-[11px] text-muted-foreground">
                0.00 is strictly deterministic. Higher produces more varied phrasing.
              </p>
            </div>

            {/* Max Tokens */}
            <div>
              <label className="block text-[12px] font-semibold uppercase tracking-wider text-muted-foreground mb-1.5">
                Max Output Tokens
              </label>
              <input
                type="number"
                min={256}
                max={32768}
                value={form.max_tokens ?? 4096}
                onChange={(e) => setForm({ ...form, max_tokens: parseInt(e.target.value, 10) || 4096 })}
                className="w-full rounded-lg border border-border bg-background px-3 py-2 text-[13px] font-mono text-foreground focus:border-brand focus:outline-none"
              />
              <p className="mt-1 text-[11px] text-muted-foreground">
                Maximum token generation length per step.
              </p>
            </div>

            {/* Context Window */}
            <div>
              <label className="block text-[12px] font-semibold uppercase tracking-wider text-muted-foreground mb-1.5">
                Context Window (LLM_NUM_CTX)
              </label>
              <input
                type="number"
                min={0}
                max={131072}
                value={form.llm_num_ctx ?? 0}
                onChange={(e) => setForm({ ...form, llm_num_ctx: parseInt(e.target.value, 10) || 0 })}
                placeholder="0 = model default"
                className="w-full rounded-lg border border-border bg-background px-3 py-2 text-[13px] font-mono text-foreground focus:border-brand focus:outline-none"
              />
              <p className="mt-1 text-[11px] text-muted-foreground">
                0 uses provider defaults. Local models support up to 32k/64k.
              </p>
            </div>

            {/* Inference Threads */}
            <div>
              <label className="block text-[12px] font-semibold uppercase tracking-wider text-muted-foreground mb-1.5">
                CPU Threads (LLM_NUM_THREAD)
              </label>
              <input
                type="number"
                min={0}
                max={128}
                value={form.llm_num_thread ?? 0}
                onChange={(e) => setForm({ ...form, llm_num_thread: parseInt(e.target.value, 10) || 0 })}
                placeholder="0 = physical core count"
                className="w-full rounded-lg border border-border bg-background px-3 py-2 text-[13px] font-mono text-foreground focus:border-brand focus:outline-none"
              />
              <p className="mt-1 text-[11px] text-muted-foreground">
                0 automatically matches your machine&apos;s physical cores.
              </p>
            </div>

            {/* Model Keep Alive */}
            <div>
              <label className="block text-[12px] font-semibold uppercase tracking-wider text-muted-foreground mb-1.5">
                Model Keep-Alive Duration
              </label>
              <input
                type="text"
                value={form.llm_keep_alive ?? "30m"}
                onChange={(e) => setForm({ ...form, llm_keep_alive: e.target.value })}
                placeholder="30m, 1h, -1 (indefinite)"
                className="w-full rounded-lg border border-border bg-background px-3 py-2 text-[13px] font-mono text-foreground focus:border-brand focus:outline-none"
              />
              <p className="mt-1 text-[11px] text-muted-foreground">
                How long the model stays loaded in VRAM/RAM between turns.
              </p>
            </div>

            {/* Max Upload MB */}
            <div>
              <label className="block text-[12px] font-semibold uppercase tracking-wider text-muted-foreground mb-1.5">
                Max Upload Limit (MB)
              </label>
              <input
                type="number"
                min={10}
                max={4096}
                value={form.max_upload_mb ?? 512}
                onChange={(e) => setForm({ ...form, max_upload_mb: parseInt(e.target.value, 10) || 512 })}
                className="w-full rounded-lg border border-border bg-background px-3 py-2 text-[13px] font-mono text-foreground focus:border-brand focus:outline-none"
              />
              <p className="mt-1 text-[11px] text-muted-foreground">
                Maximum file upload size per dataset.
              </p>
            </div>
          </div>
        </div>
      </Section>

      {/* 5. Provider Base URLs & API Keys */}
      <Section
        title="Provider Endpoints & Credentials"
        description="Configure API gateways, custom endpoints, and secure credentials saved directly to backend/.env."
      >
        <div className="space-y-4 rounded-xl border border-border bg-card p-5 shadow-xs">
          <div className="grid gap-4 sm:grid-cols-2">
            {/* Ollama URL */}
            <div>
              <label className="block text-[12px] font-semibold uppercase tracking-wider text-muted-foreground mb-1.5">
                Ollama Base URL
              </label>
              <input
                type="text"
                value={form.ollama_base_url ?? ""}
                onChange={(e) => setForm({ ...form, ollama_base_url: e.target.value })}
                placeholder="http://localhost:11434"
                className="w-full rounded-lg border border-border bg-background px-3 py-2 text-[13px] font-mono text-foreground focus:border-brand focus:outline-none"
              />
            </div>

            {/* LM Studio URL */}
            <div>
              <label className="block text-[12px] font-semibold uppercase tracking-wider text-muted-foreground mb-1.5">
                LM Studio Base URL
              </label>
              <input
                type="text"
                value={form.lmstudio_base_url ?? ""}
                onChange={(e) => setForm({ ...form, lmstudio_base_url: e.target.value })}
                placeholder="http://localhost:1234"
                className="w-full rounded-lg border border-border bg-background px-3 py-2 text-[13px] font-mono text-foreground focus:border-brand focus:outline-none"
              />
            </div>

            {/* OpenAI Gateway */}
            <div>
              <label className="block text-[12px] font-semibold uppercase tracking-wider text-muted-foreground mb-1.5">
                OpenAI Base URL
              </label>
              <input
                type="text"
                value={form.openai_base_url ?? ""}
                onChange={(e) => setForm({ ...form, openai_base_url: e.target.value })}
                placeholder="https://api.openai.com/v1"
                className="w-full rounded-lg border border-border bg-background px-3 py-2 text-[13px] font-mono text-foreground focus:border-brand focus:outline-none"
              />
            </div>

            {/* OpenAI Key */}
            <div>
              <label className="block text-[12px] font-semibold uppercase tracking-wider text-muted-foreground mb-1.5">
                OpenAI API Key
              </label>
              <input
                type="password"
                value={form.openai_api_key ?? ""}
                onChange={(e) => setForm({ ...form, openai_api_key: e.target.value })}
                placeholder="sk-..."
                className="w-full rounded-lg border border-border bg-background px-3 py-2 text-[13px] font-mono text-foreground focus:border-brand focus:outline-none"
              />
            </div>

            {/* Anthropic Gateway */}
            <div>
              <label className="block text-[12px] font-semibold uppercase tracking-wider text-muted-foreground mb-1.5">
                Anthropic Base URL
              </label>
              <input
                type="text"
                value={form.anthropic_base_url ?? ""}
                onChange={(e) => setForm({ ...form, anthropic_base_url: e.target.value })}
                placeholder="https://api.anthropic.com"
                className="w-full rounded-lg border border-border bg-background px-3 py-2 text-[13px] font-mono text-foreground focus:border-brand focus:outline-none"
              />
            </div>

            {/* Anthropic Key */}
            <div>
              <label className="block text-[12px] font-semibold uppercase tracking-wider text-muted-foreground mb-1.5">
                Anthropic API Key
              </label>
              <input
                type="password"
                value={form.anthropic_api_key ?? ""}
                onChange={(e) => setForm({ ...form, anthropic_api_key: e.target.value })}
                placeholder="sk-ant-..."
                className="w-full rounded-lg border border-border bg-background px-3 py-2 text-[13px] font-mono text-foreground focus:border-brand focus:outline-none"
              />
            </div>

            {/* Gemini Gateway */}
            <div>
              <label className="block text-[12px] font-semibold uppercase tracking-wider text-muted-foreground mb-1.5">
                Gemini Base URL
              </label>
              <input
                type="text"
                value={form.gemini_base_url ?? ""}
                onChange={(e) => setForm({ ...form, gemini_base_url: e.target.value })}
                placeholder="https://generativelanguage.googleapis.com/v1beta"
                className="w-full rounded-lg border border-border bg-background px-3 py-2 text-[13px] font-mono text-foreground focus:border-brand focus:outline-none"
              />
            </div>

            {/* Custom Gateway URL */}
            <div>
              <label className="block text-[12px] font-semibold uppercase tracking-wider text-muted-foreground mb-1.5">
                Custom Gateway URL
              </label>
              <input
                type="text"
                value={form.gateway_api_url ?? ""}
                onChange={(e) => setForm({ ...form, gateway_api_url: e.target.value })}
                placeholder="https://api.groq.com/openai/v1 or https://openrouter.ai/api/v1"
                className="w-full rounded-lg border border-border bg-background px-3 py-2 text-[13px] font-mono text-foreground focus:border-brand focus:outline-none"
              />
            </div>

            {/* Custom Gateway Key */}
            <div>
              <label className="block text-[12px] font-semibold uppercase tracking-wider text-muted-foreground mb-1.5">
                Custom Gateway API Key
              </label>
              <input
                type="password"
                value={form.gateway_api_key ?? ""}
                onChange={(e) => setForm({ ...form, gateway_api_key: e.target.value })}
                placeholder="gsk-... or sk-or-..."
                className="w-full rounded-lg border border-border bg-background px-3 py-2 text-[13px] font-mono text-foreground focus:border-brand focus:outline-none"
              />
            </div>
          </div>
        </div>
      </Section>

      {/* 7. Active Session Controls */}
      <Section
        title="Session"
        description="The server state bound to this tab. A reset clears variables, plots and the sandbox without dropping your dataset."
      >
        <div className="space-y-4 rounded-xl border border-border bg-card p-4 shadow-xs">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <p className="font-mono text-[12.5px] text-muted-foreground">
                Session: <span className="text-foreground">{sessionId ?? "—"}</span>
              </p>
              <p className="mt-1 text-[12.5px] text-muted-foreground">
                {session?.has_data ? "Dataset loaded" : "No dataset loaded"} · {session?.data_mode ?? "local-only"}
              </p>
            </div>
            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                onClick={resetWorkspace}
                disabled={busy === "reset"}
                className="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-[12.5px] font-medium transition-colors hover:bg-muted disabled:opacity-50"
              >
                {busy === "reset" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RotateCcw className="h-3.5 w-3.5" />}
                Reset session
              </button>
              <button
                type="button"
                onClick={newSession}
                disabled={busy === "session"}
                className="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-[12.5px] font-medium transition-colors hover:bg-muted disabled:opacity-50"
              >
                {busy === "session" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null}
                New session
              </button>
            </div>
          </div>
        </div>
      </Section>

      {/* 8. Permissions Matrix */}
      <Section
        title="Permissions"
        description="What the agent is allowed to do without stopping to ask."
      >
        <PermissionsPanel
          permissions={permissions}
          onProfileChange={setProfile}
          onRulingChange={setRuling}
        />
      </Section>

      {/* 9. Usage & Metering */}
      <Section
        title="Usage"
        description="Tokens and estimated costs for cloud providers in this session."
      >
        <UsagePanel usage={usage} />
      </Section>
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* Helper Subcomponents                                                       */
/* -------------------------------------------------------------------------- */

function IsolationNote({ isolation }: { isolation: string }) {
  if (isolation === "container") {
    return (
      <div className="mt-4 flex items-start gap-2.5 rounded-xl border border-success/25 bg-success/8 p-3.5 text-[13px] leading-relaxed text-success">
        <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0" />
        <span>
          Generated code runs in a container of its own, with capabilities dropped and memory, PID
          and CPU ceilings applied.
        </span>
      </div>
    )
  }

  if (isolation === "process") {
    return (
      <div className="mt-4 flex items-start gap-2.5 rounded-xl border border-border bg-muted/40 p-3.5 text-[13px] leading-relaxed text-muted-foreground">
        <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-brand" />
        <span>
          Generated code runs in a separate process with its own memory ceiling, a per-step timeout
          and a working Stop button. Static AST guard is enforced.
        </span>
      </div>
    )
  }

  return (
    <div className="mt-4 flex items-start gap-2.5 rounded-xl border border-warning/25 bg-warning/8 p-3.5 text-[13px] leading-relaxed text-warning">
      <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0" />
      <span>
        Code runs inside the API process itself with no isolation. Set{" "}
        <code className="font-mono text-[11.5px]">EXECUTION_BACKEND=host</code> for separate process isolation.
      </span>
    </div>
  )
}

function SandboxPanel({ config }: { config: ServerConfig }) {
  const [result, setResult] = useState<SandboxSelfTest | null>(null)
  const [running, setRunning] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const capability = config.sandbox_capability
  const features = capability?.features ?? []

  const runSelfTest = useCallback(async () => {
    setRunning(true)
    setError(null)
    try {
      setResult(await api.sandboxSelfTest())
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "The self-test could not be run")
    } finally {
      setRunning(false)
    }
  }, [])

  if (config.execution_backend !== "host" || !features.length) return null

  return (
    <div className="mt-4 rounded-xl border border-border bg-muted/30 p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <p className="text-[13px] font-medium text-foreground">OS Sandbox Capability</p>
          <p className="text-[12.5px] text-muted-foreground">
            {config.host_sandbox} · {capability.mechanism}
          </p>
        </div>
        <button
          type="button"
          onClick={runSelfTest}
          disabled={running || config.host_sandbox === "off"}
          className="inline-flex items-center gap-2 rounded-lg border border-border px-3 py-1.5 text-[12.5px] font-medium transition-colors hover:bg-muted disabled:opacity-50"
        >
          {running && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
          {running ? "Verifying..." : "Run Security Self-Test"}
        </button>
      </div>

      <ul className="mt-3 space-y-1.5">
        {features.map((feature) => (
          <li key={feature.key} className="flex items-start gap-2 text-[12.5px] leading-relaxed">
            {feature.supported ? (
              <ShieldCheck className="mt-0.5 h-3.5 w-3.5 shrink-0 text-success" />
            ) : (
              <ShieldAlert className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning" />
            )}
            <span className="text-muted-foreground">
              <span className="font-medium text-foreground">{feature.key}</span> — {feature.supported ? "Enforced" : "Not enforced"}: {feature.detail}
            </span>
          </li>
        ))}
      </ul>

      {error && <p className="mt-3 text-[12.5px] text-warning">{error}</p>}

      {result && (
        <div className="mt-3 border-t border-border pt-3">
          <p className={cn("text-[12.5px] font-medium", result.ok ? "text-success" : "text-warning")}>
            {result.ok ? "Verified Enforced" : "Not verified"} — {result.detail}
          </p>
        </div>
      )}
    </div>
  )
}

const PERMISSION_PROFILE_LABELS: Record<PermissionProfile, string> = {
  "auto-approve": "Auto approve",
  "ask-always": "Ask always",
  custom: "Custom",
}

const RULINGS: PermissionRuling[] = ["allow", "ask", "deny"]

function PermissionsPanel({
  permissions,
  onProfileChange,
  onRulingChange,
}: {
  permissions: PermissionsInfo | null
  onProfileChange: (profile: PermissionProfile) => Promise<void>
  onRulingChange: (key: string, ruling: PermissionRuling) => Promise<void>
}) {
  if (!permissions) {
    return <p className="text-[13px] text-muted-foreground">Loading permissions...</p>
  }

  const custom = permissions.profile === "custom"

  return (
    <div className="space-y-3">
      <div className="rounded-xl border border-border bg-card p-4 shadow-xs">
        <div
          className="flex flex-wrap items-center gap-0.5 rounded-lg bg-muted p-0.5"
          role="radiogroup"
          aria-label="Permission profile"
        >
          {(Object.keys(PERMISSION_PROFILE_LABELS) as PermissionProfile[]).map((profile) => (
            <button
              key={profile}
              type="button"
              role="radio"
              aria-checked={permissions.profile === profile}
              onClick={() => void onProfileChange(profile)}
              className={cn(
                "rounded-md px-3 py-1.5 text-[12.5px] font-medium",
                "transition-[background-color,color,box-shadow] duration-[var(--duration-fast)]",
                permissions.profile === profile
                  ? "bg-card text-foreground shadow-xs"
                  : "text-muted-foreground hover:text-foreground",
              )}
            >
              {PERMISSION_PROFILE_LABELS[profile]}
            </button>
          ))}
        </div>
        <p className="mt-3 text-[12.5px] leading-relaxed text-muted-foreground">
          {permissions.description}
        </p>
      </div>

      <div className="overflow-hidden rounded-xl border border-border bg-card shadow-xs">
        {permissions.categories.map((category) => (
          <div
            key={category.key}
            className="flex flex-wrap items-start justify-between gap-3 border-b border-border px-4 py-3.5 last:border-b-0"
          >
            <div className="min-w-0 flex-1">
              <p className="flex flex-wrap items-center gap-2 text-[13.5px] font-medium">
                {category.label}
                {category.always_ask && (
                  <span className="rounded-full bg-warning/10 px-2 py-0.5 text-[10px] font-medium text-warning">
                    Always asks
                  </span>
                )}
              </p>
              <p className="mt-1 text-[12.5px] leading-relaxed text-muted-foreground">
                {category.description}
              </p>
            </div>

            <div
              className="flex shrink-0 items-center gap-0.5 rounded-lg bg-muted p-0.5"
              role="radiogroup"
              aria-label={`${category.label} permission`}
            >
              {RULINGS.map((ruling) => {
                const forbidden = category.always_ask && ruling === "allow"
                return (
                  <button
                    key={ruling}
                    type="button"
                    role="radio"
                    aria-checked={category.ruling === ruling}
                    disabled={!custom || forbidden}
                    onClick={() => void onRulingChange(category.key, ruling)}
                    className={cn(
                      "rounded-md px-2.5 py-1 text-[11.5px] font-medium capitalize",
                      "transition-[background-color,color,box-shadow] duration-[var(--duration-fast)]",
                      "disabled:cursor-not-allowed disabled:opacity-45",
                      category.ruling === ruling
                        ? "bg-card text-foreground shadow-xs"
                        : "text-muted-foreground hover:text-foreground",
                    )}
                  >
                    {ruling}
                  </button>
                )
              })}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

function UsagePanel({ usage }: { usage: UsageTotals | null }) {
  if (!usage) return null

  if (usage.local_only) {
    return (
      <div className="rounded-xl border border-success/25 bg-success/5 p-4">
        <p className="text-[13.5px] font-medium text-success">Local Only Mode</p>
        <p className="mt-1 text-[12.5px] leading-relaxed text-muted-foreground">
          Every model runs strictly locally on this machine. Zero external API calls or token billing.
        </p>
      </div>
    )
  }

  return (
    <div className="rounded-xl border border-border bg-card p-4 shadow-xs">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <p className="text-[13.5px] font-medium">
          {usage.calls} call{usage.calls === 1 ? "" : "s"} · {usage.total_tokens.toLocaleString()} tokens
        </p>
        {usage.cost_usd !== null && (
          <p className="tabular text-[13.5px] font-medium">
            ${usage.cost_usd < 0.01 ? usage.cost_usd.toFixed(4) : usage.cost_usd.toFixed(2)}
          </p>
        )}
      </div>
    </div>
  )
}
