"use client"

import { useEffect, useState } from "react"
import { useRouter } from "next/navigation"
import { Radio, AlertTriangle } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Spinner } from "@/components/ui/spinner"
import { fetchAuthState, login, registerFirstAdmin } from "@/lib/auth"

/**
 * Login. Deliberately plain and in the current visual language — the palette
 * and type are due to be redesigned, so there is nothing here worth investing
 * in twice. It uses only tokens (bg-background, bg-card, text-primary), so a
 * change to globals.css carries it over with no edits.
 *
 * Three states, because the deployment has three:
 *  - bootstrap: no account exists yet, so this screen CREATES the first admin
 *  - normal: sign in
 *  - not enforced: auth is off on this backend, so the form would be pointless
 *    — say so and offer the way back in rather than pretending it is needed.
 */
export default function LoginPage() {
  const router = useRouter()
  const [checking, setChecking] = useState(true)
  const [bootstrap, setBootstrap] = useState(false)
  const [enforced, setEnforced] = useState(true)
  const [username, setUsername] = useState("")
  const [password, setPassword] = useState("")
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    fetchAuthState()
      .then((s) => {
        if (!alive) return
        setBootstrap(s.bootstrap)
        setEnforced(s.enforced)
        if (s.authenticated) router.replace("/")
      })
      .catch(() => {
        if (alive) setError("Cannot reach the backend.")
      })
      .finally(() => alive && setChecking(false))
    return () => {
      alive = false
    }
  }, [router])

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      if (bootstrap) {
        await registerFirstAdmin(username.trim(), password)
      } else {
        await login(username.trim(), password)
      }
      router.replace("/")
    } catch (err) {
      setError(err instanceof Error ? err.message : "Sign in failed")
      setBusy(false)
    }
  }

  if (checking) {
    return (
      <div className="flex h-screen items-center justify-center bg-background">
        <Spinner className="h-5 w-5 text-muted-foreground" />
      </div>
    )
  }

  return (
    <div className="flex h-screen items-center justify-center bg-background p-6">
      <div className="w-full max-w-sm">
        <div className="mb-8 flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary/10">
            <Radio className="h-5 w-5 text-primary" />
          </div>
          <div>
            <div className="font-semibold text-foreground">NSG AI Platform</div>
            <div className="text-xs text-muted-foreground">
              Network Scenario Generator
            </div>
          </div>
        </div>

        <div className="rounded-lg border border-border bg-card p-6">
          <h1 className="text-lg font-semibold text-card-foreground">
            {bootstrap ? "Create the first account" : "Sign in"}
          </h1>
          <p className="mt-1 mb-6 text-sm text-muted-foreground">
            {bootstrap
              ? "No account exists yet. This one becomes the administrator."
              : "Use your platform credentials."}
          </p>

          {!enforced && (
            <div className="mb-5 flex gap-2 rounded-md border border-border bg-secondary/50 p-3">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
              <p className="text-xs leading-relaxed text-muted-foreground">
                This deployment does not require a login. You can sign in to own
                your work, or{" "}
                <button
                  type="button"
                  onClick={() => router.replace("/")}
                  className="text-primary underline underline-offset-2"
                >
                  continue without an account
                </button>
                .
              </p>
            </div>
          )}

          <form onSubmit={handleSubmit} className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="username">Username</Label>
              <Input
                id="username"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                autoComplete="username"
                autoFocus
                required
                disabled={busy}
              />
            </div>

            <div className="space-y-2">
              <Label htmlFor="password">Password</Label>
              <Input
                id="password"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete={bootstrap ? "new-password" : "current-password"}
                required
                minLength={bootstrap ? 8 : undefined}
                disabled={busy}
              />
              {bootstrap && (
                <p className="text-xs text-muted-foreground">
                  At least 8 characters.
                </p>
              )}
            </div>

            {error && (
              <p className="text-sm text-destructive" role="alert">
                {error}
              </p>
            )}

            <Button type="submit" className="w-full" disabled={busy}>
              {busy && <Spinner className="mr-2 h-4 w-4" />}
              {bootstrap ? "Create account" : "Sign in"}
            </Button>
          </form>
        </div>
      </div>
    </div>
  )
}
