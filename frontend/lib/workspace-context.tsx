"use client"

import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react"

import { api, clearStoredSessionId } from "@/lib/api"
import type {
  Artifact,
  DatasetSummary,
  ServerConfig,
  SessionInfo,
  UpdateConfigPayload,
} from "@/lib/types"
import { useChatStream } from "@/lib/use-chat-stream"
import { useSound } from "@/lib/use-sound"

export type ArtifactTab = "chart" | "data" | "files"

interface WorkspaceContextValue {
  session: SessionInfo | null
  datasets: DatasetSummary[]
  activeDataset: string | null
  activeSummary: DatasetSummary | null
  config: ServerConfig | null
  uploading: boolean
  uploadError: string | null

  // Artifacts and chart panel
  panelOpen: boolean
  setPanelOpen: (open: boolean) => void
  panelTab: ArtifactTab
  setPanelTab: (tab: ArtifactTab) => void
  chartVersion: number
  chartImage: string | null

  // Actions
  uploadDataset: (file: File, clean?: boolean) => Promise<void>
  activateDataset: (name: string) => Promise<void>
  deleteDataset: (name: string) => Promise<void>
  refreshSession: () => Promise<void>
  refreshConfig: () => Promise<void>
  updateConfig: (payload: UpdateConfigPayload) => Promise<ServerConfig>
  newChat: () => Promise<void>

  // Persistent chat stream
  chat: ReturnType<typeof useChatStream>
}

const WorkspaceContext = createContext<WorkspaceContextValue | null>(null)

export function WorkspaceProvider({ children }: { children: React.ReactNode }) {
  const [session, setSession] = useState<SessionInfo | null>(null)
  const [datasets, setDatasets] = useState<DatasetSummary[]>([])
  const [activeDataset, setActiveDataset] = useState<string | null>(null)
  const [config, setConfig] = useState<ServerConfig | null>(null)
  const [uploading, setUploading] = useState(false)
  const [uploadError, setUploadError] = useState<string | null>(null)

  const [panelOpen, setPanelOpen] = useState(false)
  const [panelTab, setPanelTab] = useState<ArtifactTab>("chart")
  const [chartVersion, setChartVersion] = useState(0)
  const [chartImage, setChartImage] = useState<string | null>(null)

  const { playSound } = useSound()

  const onArtifact = useCallback((artifact: Artifact) => {
    if (artifact.kind === "plot_html") {
      setChartImage(null)
      setChartVersion((value) => value + 1)
      setPanelTab("chart")
      setPanelOpen(true)
    } else if (artifact.kind === "plot_png" && artifact.data) {
      setChartImage(`data:image/png;base64,${artifact.data}`)
      setChartVersion((value) => value + 1)
      setPanelTab("chart")
      setPanelOpen(true)
    }
  }, [])

  // The chat stream runs at the top level — it is NEVER unmounted when switching tabs!
  const chat = useChatStream({ onArtifact })

  const refreshSession = useCallback(async () => {
    try {
      const sess = await api.session()
      setSession(sess)
      setDatasets(sess.datasets || [])
      setActiveDataset(sess.active_dataset || null)
    } catch {
      setSession(null)
      setDatasets([])
      setActiveDataset(null)
    }
  }, [])

  const refreshConfig = useCallback(async () => {
    try {
      const cfg = await api.config()
      setConfig(cfg)
    } catch {
      setConfig(null)
    }
  }, [])

  const updateConfig = useCallback(async (payload: UpdateConfigPayload): Promise<ServerConfig> => {
    const updated = await api.updateConfig(payload)
    setConfig(updated)
    return updated
  }, [])

  const uploadDataset = useCallback(
    async (file: File, clean = true) => {
      setUploading(true)
      setUploadError(null)
      try {
        await api.upload(file, clean)
        await refreshSession()
        setPanelTab("data")
        setPanelOpen(true)
        playSound("click")
      } catch (exception) {
        const message = exception instanceof Error ? exception.message : "Upload failed."
        setUploadError(message)
        throw exception
      } finally {
        setUploading(false)
      }
    },
    [playSound, refreshSession],
  )

  const activateDataset = useCallback(
    async (name: string) => {
      await api.activateDataset(name)
      await refreshSession()
      playSound("click")
    },
    [playSound, refreshSession],
  )

  const deleteDataset = useCallback(
    async (name: string) => {
      await api.deleteDataset(name)
      await refreshSession()
      playSound("click")
    },
    [playSound, refreshSession],
  )

  const newChat = useCallback(async () => {
    playSound("click")
    chat.clear()
    setChartVersion(0)
    setChartImage(null)
    setPanelOpen(false)
    try {
      await api.deleteSession()
    } catch {
      // already gone
    }
    clearStoredSessionId()
    await refreshSession()
  }, [chat, playSound, refreshSession])

  useEffect(() => {
    void refreshConfig()
    void refreshSession()
  }, [refreshConfig, refreshSession])

  const activeSummary = useMemo(
    () => datasets.find((dataset) => dataset.name === activeDataset) ?? null,
    [datasets, activeDataset],
  )

  const value = useMemo<WorkspaceContextValue>(
    () => ({
      session,
      datasets,
      activeDataset,
      activeSummary,
      config,
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
      deleteDataset,
      refreshSession,
      refreshConfig,
      updateConfig,
      newChat,
      chat,
    }),
    [
      session,
      datasets,
      activeDataset,
      activeSummary,
      config,
      uploading,
      uploadError,
      panelOpen,
      panelTab,
      chartVersion,
      chartImage,
      uploadDataset,
      activateDataset,
      deleteDataset,
      refreshSession,
      refreshConfig,
      updateConfig,
      newChat,
      chat,
    ],
  )

  return <WorkspaceContext.Provider value={value}>{children}</WorkspaceContext.Provider>
}

export function useWorkspace(): WorkspaceContextValue {
  const context = useContext(WorkspaceContext)
  if (!context) {
    throw new Error("useWorkspace must be used within a WorkspaceProvider")
  }
  return context
}
