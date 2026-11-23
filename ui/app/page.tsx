
import { AuthGate } from "@/components/auth-gate"
import { NsgPlatform } from "@/components/nsg-platform"

export default function Page() {
  return (
    <AuthGate>
      <NsgPlatform />
    </AuthGate>
  )
}
