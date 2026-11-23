"use client"

import { useState } from "react"
import { HistorySidebar } from "@/components/history-sidebar"
import { ChatInterface } from "@/components/chat-interface"
import { ModelPreview } from "@/components/model-preview"
import { ConfigDiffViewer } from "@/components/ConfigDiffViewer"
import {
  ResizableHandle,
  ResizablePanel,
  ResizablePanelGroup,
} from "@/components/ui/resizable"
import { Toaster } from "@/components/ui/toaster"
import { useNSGStore } from "@/store/useNSGStore"

export function NsgPlatform() {
  const [isChatOpen, setIsChatOpen] = useState(true)
  const resetSession = useNSGStore(s => s.resetSession)

  return (
    <div className="flex h-screen bg-background">
      <HistorySidebar onNewScenario={resetSession} />

      <div className="flex flex-1 flex-col overflow-hidden">
        <ResizablePanelGroup direction="horizontal" className="flex-1">
          {isChatOpen && (
            <>
              <ResizablePanel defaultSize={28} minSize={22} maxSize={45} className="flex flex-col relative transition-all duration-300">
                <ChatInterface />
              </ResizablePanel>
              <ResizableHandle withHandle className="bg-border" />
            </>
          )}

          <ResizablePanel defaultSize={isChatOpen ? 72 : 100} minSize={30}>
            <ModelPreview isChatOpen={isChatOpen} onToggleChat={() => setIsChatOpen(!isChatOpen)} />
          </ResizablePanel>
        </ResizablePanelGroup>
      </div>

      {/* Global diff modal — reads from store directly, zero props */}
      <ConfigDiffViewer />
      <Toaster />
    </div>
  )
}
