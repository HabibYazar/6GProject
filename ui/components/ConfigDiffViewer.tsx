'use client';

/**
 * ConfigDiffViewer
 *
 * Modal that opens after the LLM proposes a config change.
 * Shows current vs. proposed side-by-side, then lets the user accept or reject.
 *
 * Data flow:
 *   useNSGStore.requestModification(prompt)
 *     → sets proposedConfig + isDiffOpen = true
 *     → this component renders
 *   User clicks Accept → useNSGStore.acceptProposal()
 *     → replaces config with proposedConfig, closes modal
 *   User clicks Reject → useNSGStore.rejectProposal()
 *     → discards proposedConfig, closes modal
 */

import ReactDiffViewer, { DiffMethod } from 'react-diff-viewer-continued';
import { CheckCircle2, XCircle, GitCompare } from 'lucide-react';

import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';

import {
  useNSGStore,
  selectConfig,
  selectProposedConfig,
  selectIsDiffOpen,
  DEFAULT_CONFIG,
} from '@/store/useNSGStore';

// ── Diff theme: matches the dark okclh palette in globals.css ─────────────────

const DIFF_STYLES = {
  variables: {
    dark: {
      diffViewerBackground:   '#18181b',
      diffViewerColor:        '#e4e4e7',
      addedBackground:        '#052e16',
      addedColor:             '#86efac',
      removedBackground:      '#450a0a',
      removedColor:           '#fca5a5',
      wordAddedBackground:    '#166534',
      wordRemovedBackground:  '#991b1b',
      gutterBackground:       '#27272a',
      gutterColor:            '#71717a',
      highlightBackground:    '#3f3f46',
      highlightGutterBackground: '#52525b',
      diffViewerTitleBackground: '#27272a',
      diffViewerTitleColor:   '#a1a1aa',
      diffViewerTitleBorderColor: '#3f3f46',
    },
  },
  // Make long JSON lines wrap rather than force horizontal scroll
  contentText: {
    whiteSpace:    'pre-wrap'   as const,
    wordBreak:     'break-word' as const,
    overflowWrap:  'anywhere'   as const,
    fontSize:      '12px',
    lineHeight:    '1.5',
  },
  line: {
    verticalAlign: 'top' as const,
  },
};

// ── Change counter ────────────────────────────────────────────────────────────

function countTopLevelChanges(
  current: object | null,
  proposed: object,
): number {
  if (!current) return Object.keys(proposed).length;
  const keys = new Set([...Object.keys(current), ...Object.keys(proposed)]);
  let count = 0;
  for (const k of keys) {
    if (JSON.stringify((current as Record<string, unknown>)[k]) !==
        JSON.stringify((proposed as Record<string, unknown>)[k])) count++;
  }
  return count;
}

// ── Component ─────────────────────────────────────────────────────────────────

export function ConfigDiffViewer() {
  const config         = useNSGStore(selectConfig);
  const proposedConfig = useNSGStore(selectProposedConfig);
  const isDiffOpen     = useNSGStore(selectIsDiffOpen);
  const acceptProposal = useNSGStore(s => s.acceptProposal);
  const rejectProposal = useNSGStore(s => s.rejectProposal);

  // Don't render until the store has a proposal ready
  if (!isDiffOpen || !proposedConfig) return null;

  const oldStr      = JSON.stringify(config ?? DEFAULT_CONFIG, null, 2);
  const newStr      = JSON.stringify(proposedConfig, null, 2);
  const changeCount = countTopLevelChanges(config, proposedConfig);

  return (
    <Dialog
      open={isDiffOpen}
      // Clicking outside rejects — no accidental accepts
      onOpenChange={(open) => { if (!open) rejectProposal(); }}
    >
      <DialogContent className="
        flex flex-col
        w-[98vw] max-w-[98vw] sm:max-w-[98vw]
        h-[96vh] max-h-[96vh]
        bg-zinc-900 border-zinc-700
        text-zinc-100
        p-0
        gap-0
      ">

        {/* ── Header ─────────────────────────────────────────────────────── */}
        <DialogHeader className="px-6 pt-5 pb-4 border-b border-zinc-700 shrink-0">
          <div className="flex items-center gap-3">
            <GitCompare className="w-5 h-5 text-teal-400 shrink-0" />
            <DialogTitle className="text-white text-lg font-semibold">
              Proposed Configuration Change
            </DialogTitle>
            {changeCount > 0 && (
              <Badge
                variant="outline"
                className="border-teal-700 text-teal-400 text-xs"
              >
                {changeCount} section{changeCount !== 1 ? 's' : ''} modified
              </Badge>
            )}
          </div>
          <DialogDescription className="text-zinc-400 text-sm mt-1">
            Review the AI-proposed changes below. Accept to update the network topology,
            or reject to keep the current configuration.
          </DialogDescription>
        </DialogHeader>

        {/* ── Diff viewer ────────────────────────────────────────────────── */}
        <div className="flex-1 overflow-auto min-h-0">
          <ReactDiffViewer
            oldValue={oldStr}
            newValue={newStr}
            splitView={true}
            compareMethod={DiffMethod.WORDS}
            leftTitle="Current Configuration"
            rightTitle="Proposed Configuration"
            useDarkTheme={true}
            styles={DIFF_STYLES}
            // Only show the changed hunks (collapsed context lines reduce noise)
            hideLineNumbers={false}
            showDiffOnly={true}
            extraLinesSurroundingDiff={3}
          />
        </div>

        {/* ── Footer ─────────────────────────────────────────────────────── */}
        <DialogFooter className="
          px-6 py-4
          border-t border-zinc-700
          bg-zinc-900
          shrink-0
          flex flex-row items-center justify-between
          sm:justify-between
        ">
          <p className="text-xs text-zinc-500 hidden sm:block">
            Accepted changes will replace the canvas topology immediately.
          </p>

          <div className="flex gap-3 ml-auto">
            <Button
              variant="outline"
              onClick={rejectProposal}
              className="
                gap-2
                border-zinc-600 text-zinc-300
                hover:bg-zinc-800 hover:text-zinc-100
              "
            >
              <XCircle className="w-4 h-4" />
              Reject
            </Button>

            <Button
              onClick={acceptProposal}
              className="
                gap-2
                bg-teal-600 hover:bg-teal-500
                text-white font-medium
              "
            >
              <CheckCircle2 className="w-4 h-4" />
              Accept Changes
            </Button>
          </div>
        </DialogFooter>

      </DialogContent>
    </Dialog>
  );
}
