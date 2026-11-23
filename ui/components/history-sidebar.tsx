"use client"

import { useEffect, useState } from "react"
import { Button } from "@/components/ui/button"
import { fetchAuthState, logout, type AuthState } from "@/lib/auth"
import { ScrollArea } from "@/components/ui/scroll-area"
import { cn } from "@/lib/utils"
import {
  ChevronLeft,
  ChevronRight,
  Plus,
  Radio,
  Save,
  Trash2,
  Layers,
  Check,
  X,
  LogOut,
  Users,
} from "lucide-react"
import { useNSGStore, selectConfig } from "@/store/useNSGStore"
import {
  listScenarios,
  saveScenario,
  deleteScenario,
  relativeTime,
  type SavedScenario,
} from "@/utils/scenarioStore"

interface HistorySidebarProps {
  onNewScenario: () => void
}

export function HistorySidebar({ onNewScenario }: HistorySidebarProps) {
  // Who is signed in, if anyone. Failure is silent on purpose: an unreachable
  // backend must not stop the sidebar from rendering saved scenarios.
  const [auth, setAuth] = useState<AuthState | null>(null)
  useEffect(() => {
    let alive = true
    fetchAuthState()
      .then((a) => alive && setAuth(a))
      .catch(() => {})
    return () => {
      alive = false
    }
  }, [])

  const [isCollapsed, setIsCollapsed] = useState(false)
  const [scenarios, setScenarios] = useState<SavedScenario[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [naming, setNaming] = useState(false)
  const [nameInput, setNameInput] = useState("")

  const config = useNSGStore(selectConfig)
  const loadConfig = useNSGStore((s) => s.loadConfig)
  const setHeatmapData = useNSGStore((s) => s.setHeatmapData)

  // Saved scenarios come from the backend now. The list can therefore fail —
  // a browser store could not — so the sidebar has to say so rather than
  // silently render empty, which reads as "you have no scenarios" when the
  // truth is "the backend is down".
  const [loadError, setLoadError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  const refresh = async () => {
    try {
      setScenarios(await listScenarios())
      setLoadError(null)
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : String(e))
    }
  }
  useEffect(() => {
    void refresh()
  }, [])

  const clone = (c: SavedScenario["config"]) =>
    typeof structuredClone === "function" ? structuredClone(c) : JSON.parse(JSON.stringify(c))

  const beginSave = () => {
    if (!config) return
    setNameInput(config.name || "Untitled scenario")
    setNaming(true)
  }
  const commitSave = async () => {
    if (!config || saving) return
    setSaving(true)
    try {
      const entry = await saveScenario(nameInput, config)
      setNaming(false)
      setSelectedId(entry.id)
      setLoadError(null)
      await refresh()
    } catch (e) {
      // The server validates the configuration before storing it, so this is
      // usually a real problem with the scenario rather than a transport error.
      setLoadError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }
  const load = (s: SavedScenario) => {
    // Through loadConfig, not setConfig: a scenario saved before a schema field
    // existed is missing it forever, and the canvas reads nested fields directly
    // and dies on a missing one. localStorage keeps old shapes indefinitely, so
    // this is the one load path guaranteed to meet them.
    void loadConfig(clone(s.config))
    setHeatmapData([]) // stale coverage doesn't apply to a freshly loaded scenario
    setSelectedId(s.id)
  }
  const remove = async (id: string) => {
    try {
      await deleteScenario(id)
      if (selectedId === id) setSelectedId(null)
      await refresh()
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : String(e))
    }
  }

  return (
    <div
      className={cn(
        "relative flex h-full flex-col border-r border-border bg-sidebar transition-all duration-300",
        isCollapsed ? "w-16" : "w-72"
      )}
    >
      {/* Header */}
      <div className="flex items-center justify-between border-b border-sidebar-border p-4">
        {!isCollapsed && (
          <div className="flex items-center gap-2">
            <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary/10">
              <Radio className="h-4 w-4 text-primary" />
            </div>
            <span className="font-semibold text-sidebar-foreground">NSG AI</span>
          </div>
        )}
        <Button
          variant="ghost"
          size="icon"
          onClick={() => setIsCollapsed(!isCollapsed)}
          className="h-8 w-8 text-sidebar-foreground hover:bg-sidebar-accent"
        >
          {isCollapsed ? <ChevronRight className="h-4 w-4" /> : <ChevronLeft className="h-4 w-4" />}
        </Button>
      </div>

      {/* Actions: New + Save */}
      <div className="flex flex-col gap-2 p-3">
        <Button
          onClick={onNewScenario}
          className={cn(
            "w-full gap-2 bg-primary text-primary-foreground shadow-[0_0_20px_rgba(45,212,191,0.3)] transition-all hover:bg-primary/90 hover:shadow-[0_0_30px_rgba(45,212,191,0.5)]",
            isCollapsed && "px-0"
          )}
        >
          <Plus className="h-4 w-4" />
          {!isCollapsed && <span>New Scenario</span>}
        </Button>
        <Button
          onClick={beginSave}
          disabled={!config}
          variant="outline"
          className={cn(
            "w-full gap-2 border-sidebar-border text-sidebar-foreground hover:bg-sidebar-accent disabled:opacity-40",
            isCollapsed && "px-0"
          )}
          title={config ? "Save the current scenario" : "Generate a scenario first"}
        >
          <Save className="h-4 w-4" />
          {!isCollapsed && <span>Save current</span>}
        </Button>

        {/* Inline name entry */}
        {naming && !isCollapsed && (
          <div className="flex items-center gap-1.5 rounded-lg border border-sidebar-border bg-background/60 p-1.5">
            <input
              autoFocus
              value={nameInput}
              onChange={(e) => setNameInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") void commitSave()
                if (e.key === "Escape") setNaming(false)
              }}
              placeholder="Scenario name"
              className="min-w-0 flex-1 bg-transparent px-1.5 text-sm text-sidebar-foreground outline-none placeholder:text-muted-foreground"
            />
            <button
              onClick={commitSave}
              className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md bg-primary/20 text-primary hover:bg-primary/30"
              title="Save"
            >
              <Check className="h-3.5 w-3.5" />
            </button>
            <button
              onClick={() => setNaming(false)}
              className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md text-muted-foreground hover:bg-sidebar-accent"
              title="Cancel"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
        )}
      </div>

      {/* Saved scenarios */}
      <ScrollArea className="flex-1 px-2">
        <div className="space-y-1 py-2">
          {loadError && !isCollapsed && (
            <div className="mx-1 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2.5 text-xs text-amber-300">
              <p className="font-medium">Scenarios unavailable</p>
              <p className="mt-1 text-amber-300/70">{loadError}</p>
              <button
                onClick={() => void refresh()}
                className="mt-2 rounded-md border border-amber-500/30 px-2 py-0.5 text-[11px] hover:bg-amber-500/10"
              >
                Retry
              </button>
            </div>
          )}
          {!loadError && scenarios.length === 0 && !isCollapsed && (
            <p className="px-3 py-6 text-center text-xs text-muted-foreground">
              No saved scenarios yet.
              <br />
              Build one, then “Save current”.
            </p>
          )}
          {scenarios.map((s) => (
            <div
              key={s.id}
              className={cn(
                "group flex w-full items-center gap-3 rounded-lg px-3 py-2.5 text-left transition-colors",
                selectedId === s.id
                  ? "bg-sidebar-accent text-sidebar-accent-foreground"
                  : "text-sidebar-foreground/70 hover:bg-sidebar-accent/50 hover:text-sidebar-foreground"
              )}
            >
              <button
                onClick={() => load(s)}
                className="flex min-w-0 flex-1 items-center gap-3 text-left"
                title="Load this scenario"
              >
                <span
                  className={cn(
                    "flex h-8 w-8 shrink-0 items-center justify-center rounded-md",
                    selectedId === s.id ? "bg-primary/20 text-primary" : "bg-muted text-muted-foreground"
                  )}
                >
                  <Layers className="h-4 w-4" />
                </span>
                {!isCollapsed && (
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm font-medium">{s.name}</p>
                    <p className="truncate text-xs text-muted-foreground">
                      {s.config.base_stations.length} cells · {relativeTime(s.savedAt)}
                    </p>
                  </div>
                )}
              </button>
              {!isCollapsed && (
                <button
                  onClick={() => void remove(s.id)}
                  className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-muted-foreground opacity-0 transition-opacity hover:bg-destructive/20 hover:text-destructive group-hover:opacity-100"
                  title="Delete scenario"
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              )}
            </div>
          ))}
        </div>
      </ScrollArea>

      {/* Footer — identity and the way out. Only rendered once /auth/me has
          answered, and only when there is something to say: a deployment with
          auth switched off has no user and should not grow a sign-out button. */}
      {!isCollapsed && (
        <div className="border-t border-sidebar-border p-4">
          {auth?.authenticated ? (
            <div className="flex items-center justify-between gap-1">
              <button
                onClick={() => { window.location.href = "/account" }}
                title="Account settings"
                className="min-w-0 flex-1 text-left"
              >
                <p className="truncate text-sm font-medium text-sidebar-foreground hover:text-primary">
                  {auth.username}
                </p>
                <p className="truncate text-xs text-muted-foreground">{auth.role}</p>
              </button>
              {auth.role === "admin" && (
                <button
                  onClick={() => { window.location.href = "/admin/users" }}
                  title="Manage users"
                  className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-muted-foreground hover:bg-sidebar-accent hover:text-sidebar-foreground"
                >
                  <Users className="h-3.5 w-3.5" />
                </button>
              )}
              <button
                onClick={() => {
                  logout()
                  window.location.href = "/login"
                }}
                title="Sign out"
                className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-muted-foreground hover:bg-sidebar-accent hover:text-sidebar-foreground"
              >
                <LogOut className="h-3.5 w-3.5" />
              </button>
            </div>
          ) : (
            <p className="text-xs text-muted-foreground">Network Scenario Generator v1.0</p>
          )}
        </div>
      )}
    </div>
  )
}
