"use client"

import { useState, useRef, useEffect } from "react"
import { Button } from "@/components/ui/button"
import { Spinner } from "@/components/ui/spinner"
import { cn } from "@/lib/utils"
import { Send, Bot, User, Sparkles, GitCompare, CheckCircle2, XCircle, SlidersHorizontal, AlertTriangle } from "lucide-react"
import {
  useNSGStore,
  selectMessages,
  selectIsModifying,
  selectProposedConfig,
  selectConfigReviewPending,
} from "@/store/useNSGStore"

export function ChatInterface() {
  const [inputValue, setInputValue] = useState("")
  const scrollAreaRef = useRef<HTMLDivElement>(null)
  const inputRef      = useRef<HTMLTextAreaElement>(null)

  const messages            = useNSGStore(selectMessages)
  const isModifying         = useNSGStore(selectIsModifying)
  const proposedConfig      = useNSGStore(selectProposedConfig)
  const reviewPending       = useNSGStore(selectConfigReviewPending)
  const requestModification = useNSGStore(s => s.requestModification)
  const acceptProposal      = useNSGStore(s => s.acceptProposal)
  const rejectProposal      = useNSGStore(s => s.rejectProposal)
  const openDiff            = useNSGStore(s => s.openDiff)
  const openConfigReview    = useNSGStore(s => s.openConfigReview)
  const dismissConfigReview = useNSGStore(s => s.dismissConfigReview)

  useEffect(() => {
    if (scrollAreaRef.current) {
      scrollAreaRef.current.scrollTop = scrollAreaRef.current.scrollHeight
    }
  }, [messages, proposedConfig, reviewPending])

  // Auto-grow the prompt box up to ~3 lines, then scroll inside it (resets to a
  // single line when cleared after send).
  useEffect(() => {
    const el = inputRef.current
    if (!el) return
    el.style.height = "auto"
    const max = 96 // ~3 lines at text-sm + py-3
    el.style.height = Math.min(el.scrollHeight, max) + "px"
    el.style.overflowY = el.scrollHeight > max ? "auto" : "hidden"
  }, [inputValue])

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    if (!inputValue.trim() || isModifying) return
    requestModification(inputValue)
    setInputValue("")
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault()
      handleSubmit(e as unknown as React.FormEvent)
    }
  }

  return (
    <div className="flex h-full flex-col">
      {/* Header */}
      <div className="flex items-center gap-3 border-b border-border px-4 py-3">
        <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary/10">
          <Sparkles className="h-5 w-5 text-primary" />
        </div>
        <div>
          <h2 className="font-semibold text-foreground">NSG Assistant</h2>
          <p className="text-xs text-muted-foreground">AI-powered network scenario generation</p>
        </div>
      </div>

      {/* Messages */}
      <div className="flex-1 overflow-y-auto p-4 custom-scrollbar" ref={scrollAreaRef}>
        <div className="space-y-4">
          {messages.length === 0 ? (
            <div className="flex h-full flex-col items-center justify-center py-12 text-center">
              <div className="mb-4 flex h-16 w-16 items-center justify-center rounded-2xl bg-primary/10">
                <Bot className="h-8 w-8 text-primary" />
              </div>
              <h3 className="mb-2 text-lg font-semibold text-foreground">Start a New Scenario</h3>
              <p className="max-w-sm text-sm text-muted-foreground">
                Describe your network scenario requirements and I&apos;ll generate a configuration for you.
              </p>
              <div className="mt-6 flex flex-wrap justify-center gap-2">
                {[
                  "Energy-saving mode for VIP users",
                  "Urban interference mitigation",
                  "Stadium high-density coverage",
                ].map((suggestion) => (
                  <button
                    key={suggestion}
                    onClick={() => requestModification(suggestion)}
                    className="rounded-full border border-border bg-card px-3 py-1.5 text-xs text-muted-foreground transition-colors hover:border-primary/50 hover:text-foreground"
                  >
                    {suggestion}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            messages.map((message) => (
              <div
                key={message.id}
                className={cn("flex gap-3", message.role === "user" ? "flex-row-reverse" : "flex-row")}
              >
                <div
                  className={cn(
                    "flex h-8 w-8 shrink-0 items-center justify-center rounded-lg",
                    message.role === "user" ? "bg-primary text-primary-foreground" : "bg-muted text-muted-foreground"
                  )}
                >
                  {message.role === "user" ? <User className="h-4 w-4" /> : <Bot className="h-4 w-4" />}
                </div>
                <div
                  className={cn(
                    "max-w-[80%] rounded-2xl px-4 py-2.5",
                    message.role === "user"
                      ? "bg-primary text-primary-foreground"
                      : "border border-border bg-card text-card-foreground"
                  )}
                >
                  <p className="text-sm leading-relaxed">{message.content}</p>
                </div>
              </div>
            ))
          )}

          {isModifying && (
            <div className="flex gap-3">
              <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-muted text-muted-foreground">
                <Bot className="h-4 w-4" />
              </div>
              <div className="flex items-center gap-2 rounded-2xl border border-border bg-card px-4 py-3">
                <Spinner className="h-4 w-4 text-primary" />
                <span className="text-sm text-muted-foreground">AI is generating scenario...</span>
              </div>
            </div>
          )}

          {/* First-scenario parameter check — the AI answered every parameter,
              and the ones the prompt never mentioned got a schema default. Ask
              once, here, before the user takes the scenario to a run. */}
          {reviewPending && !isModifying && (
            <div className="flex gap-3 animate-in slide-in-from-bottom-2">
              <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-amber-500/10 text-amber-400 border border-amber-500/20">
                <AlertTriangle className="h-4 w-4" />
              </div>
              <div className="flex-1 rounded-2xl border border-amber-500/25 bg-zinc-950/80 backdrop-blur-sm overflow-hidden">
                <div className="px-4 py-3">
                  <p className="text-sm font-semibold text-amber-200">
                    Detail parameters: keep the defaults?
                  </p>
                  <p className="mt-1 text-xs leading-relaxed text-zinc-400">
                    Your prompt set the shape of the scenario. Everything it did not
                    mention — transmit power, bandwidth, propagation, run length — is
                    on its default value. Open the config to see which ones, marked
                    with <AlertTriangle className="inline h-3 w-3 text-amber-400 align-[-1px]" />,
                    and set the ones your test depends on.
                  </p>
                </div>
                <div className="border-t border-zinc-800/60 flex">
                  <button
                    onClick={openConfigReview}
                    className="flex-1 flex items-center justify-center gap-1.5 px-3 py-2 text-xs font-medium text-amber-400 hover:bg-amber-500/10 transition-colors"
                  >
                    <SlidersHorizontal className="h-3.5 w-3.5" />
                    Review parameters
                  </button>
                  <div className="w-px bg-zinc-800/60" />
                  <button
                    onClick={dismissConfigReview}
                    className="flex-1 flex items-center justify-center gap-1.5 px-3 py-2 text-xs font-medium text-zinc-500 hover:bg-zinc-800/40 hover:text-zinc-300 transition-colors"
                  >
                    <CheckCircle2 className="h-3.5 w-3.5" />
                    Use defaults
                  </button>
                </div>
              </div>
            </div>
          )}

          {/* Pending proposal card — shown after AI responds, until accepted/discarded */}
          {proposedConfig && !isModifying && (
            <div className="flex gap-3 animate-in slide-in-from-bottom-2">
              <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-teal-500/10 text-teal-400 border border-teal-500/20">
                <Sparkles className="h-4 w-4" />
              </div>
              <div className="flex-1 rounded-2xl border border-teal-500/25 bg-zinc-950/80 backdrop-blur-sm overflow-hidden">
                {/* Review button — primary action */}
                <button
                  onClick={openDiff}
                  className="w-full flex items-center justify-between px-4 py-3 hover:bg-teal-500/5 transition-colors group"
                >
                  <div className="flex items-center gap-2">
                    <GitCompare className="h-4 w-4 text-teal-400" />
                    <span className="text-sm font-semibold text-teal-300">Review proposed changes</span>
                  </div>
                  <span className="text-xs text-zinc-500 group-hover:text-teal-400 transition-colors">View diff →</span>
                </button>

                {/* Divider + quick actions */}
                <div className="border-t border-zinc-800/60 flex">
                  <button
                    onClick={acceptProposal}
                    className="flex-1 flex items-center justify-center gap-1.5 px-3 py-2 text-xs font-medium text-teal-400 hover:bg-teal-500/10 transition-colors"
                  >
                    <CheckCircle2 className="h-3.5 w-3.5" />
                    Apply directly
                  </button>
                  <div className="w-px bg-zinc-800/60" />
                  <button
                    onClick={rejectProposal}
                    className="flex-1 flex items-center justify-center gap-1.5 px-3 py-2 text-xs font-medium text-zinc-500 hover:bg-red-950/20 hover:text-red-400 transition-colors"
                  >
                    <XCircle className="h-3.5 w-3.5" />
                    Discard
                  </button>
                </div>
              </div>
            </div>
          )}

          <div />
        </div>
      </div>

      {/* Input */}
      <div className="border-t border-border p-4">
        <form onSubmit={handleSubmit} className="relative">
          <div className="relative overflow-hidden rounded-xl border border-border bg-card shadow-sm transition-colors focus-within:border-primary/50 focus-within:ring-1 focus-within:ring-primary/20">
            <textarea
              ref={inputRef}
              value={inputValue}
              onChange={(e) => setInputValue(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="Describe your network scenario..."
              disabled={isModifying}
              rows={1}
              className="w-full resize-none bg-transparent px-4 py-3 pr-12 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none disabled:cursor-not-allowed disabled:opacity-50"
            />
            <Button
              type="submit"
              size="icon"
              disabled={!inputValue.trim() || isModifying}
              className="absolute bottom-2 right-2 h-8 w-8 rounded-lg bg-primary text-primary-foreground shadow-[0_0_15px_rgba(45,212,191,0.3)] transition-all hover:bg-primary/90 hover:shadow-[0_0_20px_rgba(45,212,191,0.5)] disabled:opacity-50 disabled:shadow-none"
            >
              <Send className="h-4 w-4" />
            </Button>
          </div>
          <p className="mt-2 text-center text-xs text-muted-foreground">
            Press Enter to send, Shift+Enter for new line
          </p>
        </form>
      </div>
    </div>
  )
}
