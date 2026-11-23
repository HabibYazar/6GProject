"use client"

import { useCallback, useEffect, useState } from "react"
import { useRouter } from "next/navigation"
import { ArrowLeft, Plus, ShieldCheck, User as UserIcon } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Spinner } from "@/components/ui/spinner"
import { fetchAuthState, type AuthState } from "@/lib/auth"
import {
  createUser,
  deleteUser,
  listUsers,
  setDisabled,
  setPassword,
  setRole,
  setUsername,
  type ManagedUser,
} from "@/lib/users"

/**
 * Administrator user management.
 *
 * There is no signup: access is granted here, or from tools/nsg_user.py on the
 * box. Plain and in the current visual language on purpose — the palette is due
 * to be replaced, so this uses only semantic tokens and will carry over.
 *
 * The backend, not this page, enforces the rules (admin-only, and the last
 * active administrator cannot be removed). Errors from it are shown verbatim
 * rather than second-guessed, so the reason a thing was refused is the real one.
 */
export default function UsersAdminPage() {
  const router = useRouter()
  const [auth, setAuth] = useState<AuthState | null>(null)
  const [users, setUsers] = useState<ManagedUser[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)

  const [newName, setNewName] = useState("")
  const [newPass, setNewPass] = useState("")
  const [creating, setCreating] = useState(false)

  const refresh = useCallback(async () => {
    try {
      setUsers(await listUsers())
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load users")
    }
  }, [])

  useEffect(() => {
    fetchAuthState()
      .then((s) => {
        setAuth(s)
        if (s.enforced && !s.authenticated) router.replace("/login")
        else void refresh()
      })
      .catch(() => setError("Cannot reach the backend."))
  }, [router, refresh])

  async function act(id: string, fn: () => Promise<unknown>, msg?: string) {
    setBusy(id)
    setError(null)
    setNotice(null)
    try {
      const r = (await fn()) as { note?: string } | undefined
      if (msg) setNotice(r?.note ? `${msg} — ${r.note}` : msg)
      await refresh()
    } catch (e) {
      setError(e instanceof Error ? e.message : "Action failed")
    } finally {
      setBusy(null)
    }
  }

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault()
    setCreating(true)
    setError(null)
    setNotice(null)
    try {
      await createUser(newName.trim(), newPass)
      setNewName("")
      setNewPass("")
      setNotice(`Created ${newName.trim()}`)
      await refresh()
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not create the user")
    } finally {
      setCreating(false)
    }
  }

  if (auth && !auth.authenticated && !auth.enforced) {
    return (
      <Shell onBack={() => router.push("/")}>
        <p className="text-sm text-muted-foreground">
          This deployment does not enforce authentication, so there are no
          accounts to manage. Set <code className="font-mono">NSG_AUTH_REQUIRED=1</code>{" "}
          on the backend to turn it on.
        </p>
      </Shell>
    )
  }

  if (auth && auth.authenticated && auth.role !== "admin") {
    return (
      <Shell onBack={() => router.push("/")}>
        <p className="text-sm text-muted-foreground">
          User management is restricted to administrators.
        </p>
      </Shell>
    )
  }

  return (
    <Shell onBack={() => router.push("/")}>
      <form
        onSubmit={handleCreate}
        className="mb-8 rounded-lg border border-border bg-card p-4"
      >
        <h2 className="mb-1 text-sm font-medium text-card-foreground">Grant access</h2>
        <p className="mb-4 text-xs text-muted-foreground">
          There is no signup — accounts are created here. New accounts start as
          standard users.
        </p>
        <div className="flex flex-wrap items-end gap-3">
          <div className="min-w-40 flex-1 space-y-1.5">
            <Label htmlFor="nu">Username</Label>
            <Input id="nu" value={newName} required disabled={creating}
                   onChange={(e) => setNewName(e.target.value)} />
          </div>
          <div className="min-w-40 flex-1 space-y-1.5">
            <Label htmlFor="np">Initial password</Label>
            <Input id="np" type="password" value={newPass} required minLength={8}
                   disabled={creating} autoComplete="new-password"
                   onChange={(e) => setNewPass(e.target.value)} />
          </div>
          <Button type="submit" disabled={creating}>
            {creating ? <Spinner className="mr-2 h-4 w-4" /> : <Plus className="mr-2 h-4 w-4" />}
            Create
          </Button>
        </div>
      </form>

      {error && (
        <p className="mb-4 text-sm text-destructive" role="alert">{error}</p>
      )}
      {notice && (
        <p className="mb-4 text-sm text-muted-foreground">{notice}</p>
      )}

      {users === null ? (
        <div className="flex justify-center py-10">
          <Spinner className="h-5 w-5 text-muted-foreground" />
        </div>
      ) : (
        <div className="overflow-hidden rounded-lg border border-border">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border bg-secondary/40 text-left text-xs text-muted-foreground">
                <th className="px-4 py-2 font-medium">User</th>
                <th className="px-4 py-2 font-medium">Role</th>
                <th className="px-4 py-2 font-medium">Last login</th>
                <th className="px-4 py-2 text-right font-medium">Actions</th>
              </tr>
            </thead>
            <tbody>
              {users.map((u) => {
                const self = u.id === auth?.user_id
                const working = busy === u.id
                return (
                  <tr key={u.id} className="border-b border-border last:border-0">
                    <td className="px-4 py-3">
                      <div className="flex items-center gap-2">
                        {u.role === "admin" ? (
                          <ShieldCheck className="h-4 w-4 text-primary" />
                        ) : (
                          <UserIcon className="h-4 w-4 text-muted-foreground" />
                        )}
                        <span className={u.disabled ? "text-muted-foreground line-through" : ""}>
                          {u.username}
                        </span>
                        {self && <span className="text-xs text-muted-foreground">(you)</span>}
                        {u.disabled && (
                          <span className="rounded bg-secondary px-1.5 py-0.5 text-xs text-muted-foreground">
                            disabled
                          </span>
                        )}
                      </div>
                    </td>
                    <td className="px-4 py-3 text-muted-foreground">{u.role}</td>
                    <td className="px-4 py-3 font-mono text-xs text-muted-foreground">
                      {u.last_login_at ? new Date(u.last_login_at).toLocaleString() : "never"}
                    </td>
                    <td className="px-4 py-3">
                      <div className="flex flex-wrap items-center justify-end gap-2">
                        {working && <Spinner className="h-4 w-4 text-muted-foreground" />}
                        {/* Renaming your own account is allowed — unlike role,
                            disable and delete, it cannot lock anyone out. */}
                        <Button variant="ghost" size="sm" disabled={working}
                          onClick={() => {
                            const n = window.prompt(`New username for ${u.username}`, u.username)
                            const next = n?.trim()
                            if (next && next !== u.username)
                              void act(u.id, () => setUsername(u.id, next),
                                       `Renamed to ${next}`)
                          }}>
                          Rename
                        </Button>
                        <Button variant="ghost" size="sm" disabled={working}
                          onClick={() => {
                            const p = window.prompt(`New password for ${u.username} (min 8 chars)`)
                            if (p) void act(u.id, () => setPassword(u.id, p), "Password changed")
                          }}>
                          Password
                        </Button>
                        <Button variant="ghost" size="sm" disabled={working || self}
                          onClick={() => void act(u.id,
                            () => setRole(u.id, u.role === "admin" ? "user" : "admin"),
                            u.role === "admin" ? "Demoted to user" : "Promoted to admin")}>
                          {u.role === "admin" ? "Make user" : "Make admin"}
                        </Button>
                        <Button variant="ghost" size="sm" disabled={working || self}
                          onClick={() => void act(u.id, () => setDisabled(u.id, !u.disabled),
                            u.disabled ? "Enabled" : "Disabled")}>
                          {u.disabled ? "Enable" : "Disable"}
                        </Button>
                        <Button variant="ghost" size="sm" disabled={working || self}
                          className="text-destructive hover:text-destructive"
                          onClick={() => {
                            if (window.confirm(
                              `Delete ${u.username}? Their runs and campaigns are kept.`))
                              void act(u.id, () => deleteUser(u.id), "Deleted")
                          }}>
                          Delete
                        </Button>
                      </div>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}

      <p className="mt-4 text-xs text-muted-foreground">
        The last active administrator cannot be demoted, disabled or deleted. If
        every admin is ever lost, use{" "}
        <code className="font-mono">tools/nsg_user.py</code> on the server.
      </p>
    </Shell>
  )
}

function Shell({ children, onBack }: { children: React.ReactNode; onBack: () => void }) {
  return (
    <div className="min-h-screen bg-background">
      <div className="mx-auto max-w-4xl p-6">
        <button onClick={onBack}
          className="mb-6 flex items-center gap-2 text-sm text-muted-foreground hover:text-foreground">
          <ArrowLeft className="h-4 w-4" />
          Back to platform
        </button>
        <h1 className="mb-6 text-xl font-semibold text-foreground">Users</h1>
        {children}
      </div>
    </div>
  )
}
