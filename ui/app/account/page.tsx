"use client"

import { useEffect, useState } from "react"
import { useRouter } from "next/navigation"
import { ArrowLeft, KeyRound } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Spinner } from "@/components/ui/spinner"
import { changeOwnPassword, fetchAuthState, type AuthState } from "@/lib/auth"

/**
 * Your own account. One thing on it: change your password.
 *
 * The only verification is the CURRENT password, deliberately. Anything else a
 * reset flow might ask for — an email address, a username — is not secret, so
 * accepting it would hand the account to anyone who knows those facts. A
 * forgotten password is an administrator's job here, because this is an
 * appliance with no signup and usually no outbound mail; the page says so rather
 * than leaving the user looking for a "forgot password" link that cannot exist.
 */
export default function AccountPage() {
  const router = useRouter()
  const [auth, setAuth] = useState<AuthState | null>(null)
  const [current, setCurrent] = useState("")
  const [next, setNext] = useState("")
  const [confirm, setConfirm] = useState("")
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState<string | null>(null)

  useEffect(() => {
    fetchAuthState()
      .then((s) => {
        setAuth(s)
        if (s.enforced && !s.authenticated) router.replace("/login")
      })
      .catch(() => setError("Cannot reach the backend."))
  }, [router])

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    setError(null)
    setDone(null)
    if (next !== confirm) {
      setError("The new passwords do not match.")
      return
    }
    setBusy(true)
    try {
      const r = await changeOwnPassword(current, next)
      setDone(r.note ? `Password changed — ${r.note}` : "Password changed.")
      setCurrent("")
      setNext("")
      setConfirm("")
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not change the password")
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="min-h-screen bg-background">
      <div className="mx-auto max-w-md p-6">
        <button
          onClick={() => router.push("/")}
          className="mb-6 flex items-center gap-2 text-sm text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="h-4 w-4" />
          Back to platform
        </button>

        <h1 className="text-xl font-semibold text-foreground">Account</h1>
        {auth?.authenticated && (
          <p className="mb-6 mt-1 text-sm text-muted-foreground">
            Signed in as{" "}
            <span className="text-foreground">{auth.username}</span> ({auth.role})
          </p>
        )}

        <form onSubmit={submit} className="rounded-lg border border-border bg-card p-5">
          <div className="mb-4 flex items-center gap-2">
            <KeyRound className="h-4 w-4 text-primary" />
            <h2 className="text-sm font-medium text-card-foreground">
              Change password
            </h2>
          </div>

          <div className="space-y-4">
            <div className="space-y-1.5">
              <Label htmlFor="cur">Current password</Label>
              <Input id="cur" type="password" value={current} required disabled={busy}
                     autoComplete="current-password"
                     onChange={(e) => setCurrent(e.target.value)} />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="new">New password</Label>
              <Input id="new" type="password" value={next} required minLength={8}
                     disabled={busy} autoComplete="new-password"
                     onChange={(e) => setNext(e.target.value)} />
              <p className="text-xs text-muted-foreground">At least 8 characters.</p>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="cnf">Confirm new password</Label>
              <Input id="cnf" type="password" value={confirm} required disabled={busy}
                     autoComplete="new-password"
                     onChange={(e) => setConfirm(e.target.value)} />
            </div>

            {error && <p className="text-sm text-destructive" role="alert">{error}</p>}
            {done && <p className="text-sm text-muted-foreground">{done}</p>}

            <Button type="submit" disabled={busy} className="w-full">
              {busy && <Spinner className="mr-2 h-4 w-4" />}
              Change password
            </Button>
          </div>
        </form>

        <p className="mt-4 text-xs text-muted-foreground">
          Forgotten your password? There is no self-service reset: this platform
          has no signup and, on an on-prem appliance, often no outbound mail. Ask
          an administrator to set a new one.
        </p>
      </div>
    </div>
  )
}
