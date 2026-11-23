"use client"

import { useEffect, useState } from "react"
import { useRouter } from "next/navigation"

import { Spinner } from "@/components/ui/spinner"
import { fetchAuthState, type AuthState } from "@/lib/auth"

/**
 * Decides whether the app may render.
 *
 * The backend, not the browser, is the authority: /auth/me reports both who the
 * caller is and whether this deployment enforces auth at all. That second half
 * matters — with enforcement off the platform must work exactly as it always
 * has, logged out, so this gate lets everything through rather than inventing a
 * login requirement the API does not have.
 *
 * A backend that cannot be reached is NOT treated as "unauthorised": the app
 * renders and the individual calls surface their own errors, which is the
 * behaviour that existed before auth and is far less confusing than a login
 * screen for a server that is simply down.
 */
export function AuthGate({ children }: { children: React.ReactNode }) {
  const router = useRouter()
  const [state, setState] = useState<AuthState | null>(null)
  const [settled, setSettled] = useState(false)

  useEffect(() => {
    let alive = true
    fetchAuthState()
      .then((s) => {
        if (!alive) return
        setState(s)
        if (s.enforced && !s.authenticated) router.replace("/login")
      })
      .catch(() => {
        /* backend unreachable — fall through and let the app show its errors */
      })
      .finally(() => alive && setSettled(true))
    return () => {
      alive = false
    }
  }, [router])

  if (!settled) {
    return (
      <div className="flex h-screen items-center justify-center bg-background">
        <Spinner className="h-5 w-5 text-muted-foreground" />
      </div>
    )
  }

  // Redirecting: render nothing rather than flashing the app for a moment.
  if (state?.enforced && !state.authenticated) return null

  return <>{children}</>
}
