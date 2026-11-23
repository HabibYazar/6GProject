import React, { useState, useEffect, useMemo } from 'react';
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Search, Save, ChevronRight, Info, Plus, Trash2, AlertTriangle, Download } from 'lucide-react';
import { fetchSchema, normalizeConfig } from "@/lib/api";

/**
 * Config editor, driven by the backend's JSON Schema.
 *
 * The previous version walked the CONFIG OBJECT and picked dropdown options
 * from a hand-written if/else chain keyed on field name. Two consequences:
 *
 *   1. A parameter absent from the config JSON — which is most of them, since
 *      almost every field has a default — was INVISIBLE. The user could only
 *      edit what someone had already typed.
 *   2. The hand-written option lists had drifted from the schema. It offered
 *      'URLLC' where the schema says 'uRLLC', 'MCS' where the schema says
 *      'error_model', 'UMi-StreetCanyon' where the schema says 'UMi'. Picking
 *      one of those produced a 422 from the backend.
 *
 * Walking the SCHEMA instead fixes both at once, permanently: every field the
 * backend defines shows up, every dropdown offers exactly the values the
 * backend accepts, and a field added to schema.py appears here with no UI
 * change at all.
 */

// A bounded integer with no more than this many possible values is offered as
// a dropdown rather than a number box — picking from 7 transmission modes is
// easier than knowing that 0..6 is the range.
const DISCRETE_MAX_OPTIONS = 12;

/** Follow a $ref into $defs. */
function deref(node, root) {
  let n = node;
  let guard = 0;
  while (n && n.$ref && guard++ < 10) {
    const key = n.$ref.replace('#/$defs/', '');
    const target = root?.$defs?.[key];
    if (!target) break;
    // Keep the sibling keys (default, description) that sit next to a $ref.
    const { $ref, ...rest } = n;
    n = { ...target, ...rest };
  }
  return n || {};
}

/**
 * Optional fields arrive as anyOf: [realType, {type:'null'}]. Collapse to the
 * real branch so a nullable number still renders as a number.
 */
function unwrap(node, root) {
  let n = deref(node, root);
  if (Array.isArray(n.anyOf)) {
    const real = n.anyOf.map(b => deref(b, root)).find(b => b.type !== 'null');
    if (real) {
      n = { ...real };
      if (node.default !== undefined) n.default = node.default;
      if (node.description) n.description = node.description;
    }
  }
  return n;
}

/** What widget a field wants, decided from its schema alone. */
function widgetFor(node) {
  if (Array.isArray(node.enum)) return { kind: 'enum', options: node.enum };
  if (node.type === 'boolean') return { kind: 'enum', options: [true, false] };
  if (node.type === 'integer'
      && typeof node.minimum === 'number' && typeof node.maximum === 'number'
      && node.maximum - node.minimum + 1 <= DISCRETE_MAX_OPTIONS) {
    const opts = [];
    for (let v = node.minimum; v <= node.maximum; v++) opts.push(v);
    return { kind: 'enum', options: opts };
  }
  if (node.type === 'number' || node.type === 'integer') return { kind: 'number' };
  if (node.type === 'array') return { kind: 'array' };
  if (node.type === 'object' || node.properties) return { kind: 'object' };
  return { kind: 'text' };
}

function rangeLabel(node) {
  const lo = node.minimum, hi = node.maximum;
  if (typeof lo === 'number' && typeof hi === 'number') return `${lo} – ${hi}`;
  if (typeof lo === 'number') return `≥ ${lo}`;
  if (typeof hi === 'number') return `≤ ${hi}`;
  return null;
}

const labelFor = (key) => key.replace(/_/g, ' ');

/**
 * A new array item, built from the item schema.
 *
 * Required fields must be filled or the backend rejects the config on Sync;
 * optional ones are left out so they keep showing as "default" rather than
 * being frozen at whatever the UI guessed.
 */
function blankItem(itemSchema, root, config) {
  const props = itemSchema.properties || {};
  const required = new Set(itemSchema.required || []);
  const out = {};
  for (const k of Object.keys(props)) {
    const n = unwrap(props[k], root);
    if (n.default !== undefined) { out[k] = n.default; continue; }
    // A nested model declared with pydantic's `default_factory` appears with
    // NEITHER a `default` NOR a place in `required` — the schema is saying "the
    // backend will build this for me". True for the backend; false here, because
    // Sync writes straight into the store. `radio.antenna` and `radio.sleep` are
    // exactly this, and a base station added from this editor arrived with no
    // antenna at all: the canvas then died on `bs.radio.antenna.type`.
    const isNestedModel = !!n.properties || n.type === 'object';
    if (!required.has(k) && !isNestedModel) continue;
    if (Array.isArray(n.enum) && n.enum.length) out[k] = n.enum[0];
    else if (n.x_options_from) out[k] = optionsFrom(n.x_options_from, config)[0] ?? "";
    // Inside the declared range, not 0. A required number with a minimum — a
    // band's frequency_mhz (>= 100) and bandwidth_mhz (>= 1) — was created as 0,
    // so "add a base station" produced an item that could never Sync and the user
    // had to find two fields to make the button's own output legal.
    else if (n.type === 'number' || n.type === 'integer') {
      out[k] = typeof n.minimum === 'number' ? n.minimum
             : typeof n.maximum === 'number' ? Math.min(0, n.maximum)
             : 0;
    }
    else if (n.type === 'boolean') out[k] = false;
    else if (n.type === 'array') out[k] = [];
    else if (n.type === 'object' || n.properties) out[k] = blankItem(n, root, config);
    else out[k] = "";
  }
  return out;
}

/**
 * Resolve an "x_options_from" pointer like "base_stations[].id" against the
 * config, so a cross-referencing field can offer the values that exist.
 * JSON Schema cannot express "one of the ids in that other array", and the
 * alternative — a free text box — lets a typo through to a fault that silently
 * never fires.
 */
function optionsFrom(pointer, config) {
  if (!pointer || !config) return [];
  const [arrPart, field] = pointer.split('.');
  const arrKey = arrPart.replace('[]', '');
  const arr = config[arrKey];
  if (!Array.isArray(arr)) return [];
  return arr.map(x => x?.[field]).filter(v => v !== undefined && v !== null);
}

/**
 * Is this field still carrying the value the schema would have picked for it?
 *
 * An AI-generated scenario answers EVERY parameter, including the hundreds the
 * user's prompt never mentioned — those answers are the schema defaults. This
 * is what separates "the AI decided 46 dBm" from "nobody decided, so it's 43".
 * A deliberate choice that lands on the default reads as a default here, which
 * is the harmless direction: it invites a look at a value that is already right.
 */
function isAtDefault(node, value) {
  if (value === undefined || value === null) return true;
  if (node.default === undefined) return false;
  return JSON.stringify(value) === JSON.stringify(node.default);
}

/** A parameter the backend flagged as outcome-deciding (schema.py KEY_PARAMETERS). */
const isKeyParam = (node) => node?.x_key === true;

/**
 * A field that is real on the fast analytical tier but never reaches the real
 * ns-O-RAN engine (schema.py TIER_SCOPED). Five of these are also key
 * parameters, so without saying so the editor would urge the user to set a
 * value the real engine ignores.
 */
// WHO READS THIS FIELD — measured, not believed.
// `x_tier` was a single "analytical" flag naming a tier that no longer
// exists; `x_reads` is the full answer from `param_audit --consumers`,
// which perturbs every field and diffs all three consumers. 119 of 165
// fields turn out to be SLOW TIER ONLY, and the editor used to say that
// about a dozen of them.
const TIER_LABEL = {
  slow: ['slow tier', 'The ns-3 engine reads this. The fast tier has no such lever.'],
  fast: ['fast tier', 'The fast tier reads this. ns-3 never sees it.'],
  coverage: ['coverage map', 'Only the REM coverage map reads this.'],
  label: ['label', 'A label, not a lever — measured, changing it moves nothing.'],
  partial: ['partly on fast', 'Some of its values reach the fast tier and some do not — see the reason.'],
};

/** The consumer chip: `x_tier` when a field has a written reason, otherwise
 *  the measured `x_reads` set, and nothing when everything reads it. */
const consumerChip = (node) => {
  if (node?.x_tier && TIER_LABEL[node.x_tier]) {
    const [text, why] = TIER_LABEL[node.x_tier];
    return [text, `${why} ${node.x_tier_reason ?? ''}`.trim()];
  }
  const reads = node?.x_reads;
  if (!Array.isArray(reads)) return null;
  if (reads.length === 0) return ['read by nothing', 'Measured: no consumer reads this field.'];
  // Everything reads it — no chip is the quiet, common case.
  if (reads.includes('slow') && reads.includes('fast')) return null;
  if (reads.length === 1 && TIER_LABEL[reads[0]]) return TIER_LABEL[reads[0]];
  return [reads.join(' + '), `Measured: read by ${reads.join(' and ')}.`];
};

const isTierScoped = (node) => consumerChip(node) !== null;

export default function ConfigEditor({ currentConfig, onConfigSave, reviewSignal = 0 }) {
  const [localConfig, setLocalConfig] = useState(null);
  const [schema, setSchema] = useState(null);
  const [schemaError, setSchemaError] = useState(null);
  const [searchTerm, setSearchTerm] = useState("");
  const [open, setOpen] = useState({});
  const [helpFor, setHelpFor] = useState(null);
  /** Show only key parameters still on their default — the "what did the AI
   *  assume?" view the chat sends the user here for. */
  const [reviewOnly, setReviewOnly] = useState(false);
  /**
   * The fields the review view is showing, frozen when it opens.
   *
   * Without this the filter eats its own contents: the first keystroke moves a
   * field off its default, the field stops needing review, and it disappears
   * from under the cursor mid-edit. The list to work through is decided once;
   * the amber marks inside it then clear one by one as they are answered.
   */
  const [reviewFrozen, setReviewFrozen] = useState(null);
  const [syncing, setSyncing] = useState(false);
  const [syncError, setSyncError] = useState(null);
  /** Local edits not yet pushed to the scenario. The editor holds a draft, so
   *  switching to the Visual tab before Sync shows the OLD scenario — which
   *  reads as "my change did not apply". Say so instead. */
  const [dirty, setDirty] = useState(false);

  // Arriving from the chat's "Review parameters" card opens straight into the
  // filtered view; the user can widen it back to everything from the header.
  // A counter, not a boolean, so a second visit re-focuses the filter after the
  // user has switched it off.
  useEffect(() => {
    if (reviewSignal > 0) { setReviewOnly(true); setReviewFrozen(null); }
  }, [reviewSignal]);

  useEffect(() => {
    if (currentConfig) {
      setLocalConfig(JSON.parse(JSON.stringify(currentConfig)));
      setDirty(false);
      setSyncError(null);
    }
  }, [currentConfig]);

  useEffect(() => {
    fetchSchema()
      .then(setSchema)
      .catch(e => setSchemaError(String(e?.message || e)));
  }, []);

  /**
   * Push the draft to the scenario, but through the BACKEND first so it comes
   * back complete: defaults filled, nested models built, ranges checked. Editing
   * a plain object cannot produce what pydantic would have produced, and the
   * store is consumed by the canvas, which crashes on a missing sub-object
   * rather than shrugging.
   */
  const sync = async () => {
    if (!localConfig || syncing) return;
    setSyncing(true);
    try {
      const { config } = await normalizeConfig(localConfig);
      onConfigSave(config);
      setSyncError(null);
      setDirty(false);
    } catch (e) {
      const raw = e instanceof Error ? e.message : String(e);
      let friendly = raw;
      try {
        const parsed = JSON.parse(raw);
        if (Array.isArray(parsed?.errors) && parsed.errors.length) {
          friendly = parsed.errors.slice(0, 4)
            .map(er => `${(er.loc ?? []).join('.')}: ${er.msg}`).join(' · ');
        } else if (parsed?.message) {
          friendly = parsed.message;
        }
      } catch { /* not JSON — show as-is */ }
      setSyncError(friendly);
    } finally {
      setSyncing(false);
    }
  };

  /** Write a value at a path, creating the intermediate containers it needs. */
  const setAt = (path, value) => {
    setDirty(true);
    setLocalConfig(prev => {
      const next = JSON.parse(JSON.stringify(prev ?? {}));
      let cur = next;
      for (let i = 0; i < path.length - 1; i++) {
        const k = path[i];
        if (cur[k] === undefined || cur[k] === null) {
          cur[k] = typeof path[i + 1] === 'number' ? [] : {};
        }
        cur = cur[k];
      }
      cur[path[path.length - 1]] = value;
      return next;
    });
  };

  const getAt = (path) => {
    let cur = localConfig;
    for (const k of path) {
      if (cur === undefined || cur === null) return undefined;
      cur = cur[k];
    }
    return cur;
  };

  const term = searchTerm.trim().toLowerCase();

  const matches = (key, node) => {
    if (!term) return true;
    return key.toLowerCase().includes(term)
      || (node.description || '').toLowerCase().includes(term)
      || (node.title || '').toLowerCase().includes(term);
  };

  /** A field needs reviewing when it decides the outcome and nobody chose it. */
  const needsReview = (node, path) => isKeyParam(node) && isAtDefault(node, getAt(path));

  const leafVisible = (key, node, path) => {
    if (!matches(key, node)) return false;
    if (!reviewOnly) return true;
    // Frozen list once the view is open (see reviewFrozen); live until then, so
    // the first render before the freeze still shows the right fields.
    return reviewFrozen ? reviewFrozen.has(path.join('.')) : needsReview(node, path);
  };

  /**
   * Does this subtree contain anything the current filters let through?
   *
   * Containers used to be kept or dropped by peeking two levels down, which was
   * enough for a search box and not for a filter that can leave one matching
   * field at the bottom of a nested array. Recursing is the same question asked
   * properly, and it is what makes "show me only what needs a decision" collapse
   * the other ~570 parameters instead of showing their empty groups.
   */
  const nodeVisible = (rawNode, path, depth = 0) => {
    if (depth > 8) return false;
    const node = unwrap(rawNode, schema);
    const w = widgetFor(node);
    const key = path[path.length - 1];

    if (w.kind === 'object') {
      const props = node.properties || {};
      return Object.keys(props).some(k => nodeVisible(props[k], [...path, k], depth + 1));
    }
    if (w.kind === 'array') {
      const itemSchema = unwrap(node.items || {}, schema);
      if (!itemSchema.properties) return leafVisible(key, node, path);
      const items = getAt(path);
      if (!Array.isArray(items)) return false;
      return items.some((_, i) =>
        Object.keys(itemSchema.properties).some(k =>
          nodeVisible(itemSchema.properties[k], [...path, i, k], depth + 1)));
    }
    return leafVisible(key, node, path);
  };

  /** Every key parameter still on a default, as dotted paths. */
  const reviewIds = useMemo(() => {
    if (!schema || !localConfig) return [];
    const out = [];
    const walk = (rawNode, path, depth) => {
      if (depth > 8) return;
      const node = unwrap(rawNode, schema);
      const w = widgetFor(node);
      if (w.kind === 'object') {
        const props = node.properties || {};
        for (const k of Object.keys(props)) walk(props[k], [...path, k], depth + 1);
      } else if (w.kind === 'array') {
        const itemSchema = unwrap(node.items || {}, schema);
        const items = getAt(path);
        if (!itemSchema.properties || !Array.isArray(items)) return;
        items.forEach((_, i) => {
          for (const k of Object.keys(itemSchema.properties)) {
            walk(itemSchema.properties[k], [...path, i, k], depth + 1);
          }
        });
      } else if (needsReview(node, path)) {
        out.push(path.join('.'));
      }
    };
    walk(schema, [], 0);
    return out;
  }, [schema, localConfig]);

  const reviewCount = reviewIds.length;

  // Freeze the working list the first time the view can be computed properly —
  // the schema arrives from the backend, so the first render has nothing to
  // freeze yet.
  useEffect(() => {
    if (reviewOnly && !reviewFrozen && schema && localConfig) {
      setReviewFrozen(new Set(reviewIds));
    }
    if (!reviewOnly && reviewFrozen) setReviewFrozen(null);
  }, [reviewOnly, reviewFrozen, schema, localConfig, reviewIds]);

  // Opened with nothing to review — drop back to the full editor rather than
  // showing an empty screen that looks like a load failure.
  useEffect(() => {
    if (reviewOnly && reviewFrozen && reviewFrozen.size === 0) setReviewOnly(false);
  }, [reviewOnly, reviewFrozen]);

  const renderLeaf = (key, node, path, value) => {
    // A field that points at another part of the config becomes a dropdown of
    // what is actually there — see optionsFrom.
    const refOpts = node.x_options_from ? optionsFrom(node.x_options_from, localConfig) : null;
    const w = refOpts && refOpts.length
      ? { kind: 'enum', options: refOpts }
      : widgetFor(node);
    const id = path.join('.');
    const unset = value === undefined || value === null;
    const shown = unset ? node.default : value;
    const range = rangeLabel(node);
    const usingDefault = isAtDefault(node, value);
    const tierScoped = isTierScoped(node);
    // The whole point of the review flow: an outcome-deciding parameter that
    // nobody actually chose. Marked in place so it catches the eye in the full
    // editor too, not only inside the filtered view. A tier-scoped field is
    // never flagged — pressing the user to decide a value the real engine
    // discards is exactly the false urgency this flow is meant to remove.
    const flagged = isKeyParam(node) && usingDefault && !tierScoped;

    // The scenario description is a PARAGRAPH — the generator explains there why
    // it chose the counts, where it put the crossover and what the window budget
    // buys. In a 176px right-aligned monospace box that is unreadable, which is
    // the same as not being written. It gets its own full-width field below the
    // label instead. Keyed on the name because it is the only long-form string
    // in the schema; if another appears, add it here.
    const longForm = key === 'description';

    return (
      <div
        key={id}
        className={`group px-2 py-1.5 rounded hover:bg-white/5 transition-colors ${
          flagged ? 'bg-amber-500/[0.04] border-l-2 border-amber-500/40' : ''
        }`}
      >
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-1.5 min-w-0">
            {flagged && (
              <span title="Key parameter — still on its default value. If your scenario depends on it, set it yourself.">
                <AlertTriangle className="w-3 h-3 text-amber-400 shrink-0" />
              </span>
            )}
            <label className={`text-xs truncate ${
              flagged ? 'text-amber-100/80 group-hover:text-amber-50' : 'text-white/60 group-hover:text-white/90'
            }`}>
              {labelFor(key)}
            </label>
            {node.description && (
              <button
                type="button"
                onClick={() => setHelpFor(helpFor === id ? null : id)}
                className="opacity-0 group-hover:opacity-100 transition-opacity shrink-0"
                title="What this does"
              >
                <Info className="w-3 h-3 text-white/30 hover:text-teal-400" />
              </button>
            )}
            {usingDefault && (
              <span className="text-[9px] text-white/20 uppercase tracking-wide shrink-0">
                default
              </span>
            )}
            {tierScoped && (() => {
              const [text, why] = consumerChip(node);
              return (
                <span
                  title={why}
                  className="text-[9px] text-sky-400/60 uppercase tracking-wide shrink-0 border border-sky-500/25 rounded px-1"
                >
                  {text}
                </span>
              );
            })()}
          </div>

          {w.kind === 'enum' ? (
            <select
              value={String(shown)}
              onChange={e => {
                const raw = e.target.value;
                const picked = w.options.find(o => String(o) === raw);
                setAt(path, picked);
              }}
              className="bg-zinc-900 border border-white/10 text-[11px] rounded px-2 py-1 outline-none focus:border-teal-500 w-44 shrink-0"
            >
              {w.options.map(o => (
                <option key={String(o)} value={String(o)}>
                  {/* x_enum_labels is ours: it turns a bare 5QI into
                      "82 — Discrete automation, small packets". */}
                  {node.x_enum_labels?.[String(o)] ?? String(o)}
                </option>
              ))}
            </select>
          ) : w.kind === 'number' ? (
            <div className="flex items-center gap-2 shrink-0">
              {range && <span className="text-[9px] text-white/25 font-mono">{range}</span>}
              <input
                type="number"
                value={shown ?? ''}
                min={node.minimum}
                max={node.maximum}
                step={node.type === 'integer' ? 1 : 'any'}
                onChange={e => setAt(path, e.target.value === '' ? null : Number(e.target.value))}
                className="bg-transparent border-b border-white/10 text-[11px] text-right focus:border-teal-500 outline-none w-28 px-1 text-teal-400 font-mono"
              />
            </div>
          ) : longForm ? null : (
            <input
              type="text"
              value={shown ?? ''}
              onChange={e => setAt(path, e.target.value)}
              className="bg-transparent border-b border-white/10 text-[11px] text-right focus:border-teal-500 outline-none w-44 px-1 text-teal-400 font-mono shrink-0"
            />
          )}
        </div>

        {longForm && (
          <textarea
            rows={4}
            value={shown ?? ''}
            onChange={e => setAt(path, e.target.value)}
            className="mt-1.5 w-full bg-zinc-900/60 border border-white/10 rounded px-2 py-1.5 text-[11px] leading-relaxed text-white/75 outline-none focus:border-teal-500 resize-y"
          />
        )}

        {helpFor === id && node.description && (
          <p className="mt-1.5 ml-1 text-[10px] leading-relaxed text-white/45 whitespace-pre-line border-l border-teal-500/30 pl-2">
            {node.description}
          </p>
        )}
      </div>
    );
  };

  // While a filter is on, a collapsed group hides the very fields the filter
  // exists to surface — so filtering expands everything it kept.
  const filtering = !!term || reviewOnly;

  const renderObject = (key, node, path, depth) => {
    const props = node.properties || {};
    const id = path.join('.') || 'root';
    const isOpen = filtering ? true : (open[id] ?? (depth < 1));

    const childKeys = Object.keys(props).filter(
      k => nodeVisible(props[k], [...path, k], depth + 1));
    if (childKeys.length === 0) return null;

    const body = childKeys.map(k => renderNode(k, props[k], [...path, k], depth + 1));
    if (depth === 0) return <div key={id}>{body}</div>;

    return (
      <div key={id} className="my-1">
        <button
          type="button"
          onClick={() => setOpen(o => ({ ...o, [id]: !isOpen }))}
          className="flex items-center gap-1 w-full text-left px-1 py-1 hover:bg-white/5 rounded"
        >
          <ChevronRight
            className={`w-3 h-3 text-white/30 transition-transform ${isOpen ? 'rotate-90' : ''}`}
          />
          <span className="text-[10px] font-bold text-white/40 uppercase tracking-tight">
            {labelFor(key)}
          </span>
          <span className="text-[9px] text-white/20 ml-1">{childKeys.length}</span>
        </button>
        {isOpen && <div className="ml-3 pl-2 border-l border-white/5">{body}</div>}
      </div>
    );
  };

  const renderArray = (key, node, path, depth) => {
    const itemSchema = unwrap(node.items || {}, schema);
    const value = getAt(path);
    const items = Array.isArray(value) ? value : [];
    const id = path.join('.');
    const isOpen = filtering ? true : (open[id] ?? false);

    // Arrays of scalars are rare here; show them as a read-through text box.
    if (!itemSchema.properties) {
      return renderLeaf(key, { ...node, type: 'text' }, path,
                        items.length ? items.join(', ') : undefined);
    }

    const addItem = () => {
      const next = [...items, blankItem(itemSchema, schema, localConfig)];
      setAt(path, next);
      setOpen(o => ({ ...o, [id]: true }));
    };
    const removeItem = (i) => setAt(path, items.filter((_, j) => j !== i));

    // Under a filter, only the entries that still hold something matching —
    // otherwise "3 gNBs, one of which has an unset azimuth" shows all three.
    const itemKeys = (i) => Object.keys(itemSchema.properties)
      .filter(k => nodeVisible(itemSchema.properties[k], [...path, i, k], depth + 1));
    const visibleItems = items
      .map((_, i) => i)
      .filter(i => itemKeys(i).length > 0);

    return (
      <div key={id} className="my-1">
        <div className="flex items-center gap-1 px-1 py-1 hover:bg-white/5 rounded">
          <button
            type="button"
            onClick={() => setOpen(o => ({ ...o, [id]: !isOpen }))}
            className="flex items-center gap-1 flex-1 text-left"
          >
            <ChevronRight
              className={`w-3 h-3 text-white/30 transition-transform ${isOpen ? 'rotate-90' : ''}`}
            />
            <span className="text-[10px] font-bold text-white/40 uppercase tracking-tight">
              {labelFor(key)}
            </span>
            <span className="text-[9px] text-white/20 ml-1">{items.length}</span>
          </button>
          {/* "Add an entry to anomalies", not a naive singularisation — that
              produced "Add a anomalie". */}
          <button
            type="button"
            onClick={addItem}
            title={`Add an entry to ${labelFor(key)}`}
            className="flex items-center gap-1 text-[9px] uppercase tracking-wide text-white/35 hover:text-teal-400 px-1.5 py-0.5 rounded hover:bg-white/5"
          >
            <Plus className="w-3 h-3" /> add
          </button>
        </div>
        {isOpen && (
          <div className="ml-3 pl-2 border-l border-white/5">
            {visibleItems.map((i) => (
              <div key={i} className="my-1">
                <div className="flex items-center justify-between px-1 group/item">
                  <span className="text-[9px] text-white/25 uppercase">
                    {/* The item's own name/id, when it has one — "gnb-site-a-s1"
                        tells the user which cell they are editing; "[0]" does not. */}
                    {labelFor(key)} [{i}]
                    {(items[i]?.name || items[i]?.id) && (
                      <span className="ml-1.5 normal-case text-white/40">
                        {items[i].name || items[i].id}
                      </span>
                    )}
                  </span>
                  <button
                    type="button"
                    onClick={() => removeItem(i)}
                    title="Remove"
                    className="opacity-0 group-hover/item:opacity-100 transition-opacity text-white/30 hover:text-red-400"
                  >
                    <Trash2 className="w-3 h-3" />
                  </button>
                </div>
                <div className="ml-2 pl-2 border-l border-white/5">
                  {itemKeys(i).map(k =>
                    renderNode(k, itemSchema.properties[k], [...path, i, k], depth + 1))}
                </div>
              </div>
            ))}
            {items.length === 0 && !filtering && (
              <p className="text-[10px] text-white/25 px-1 py-1">
                none yet — use “add” above
              </p>
            )}
          </div>
        )}
      </div>
    );
  };

  const renderNode = (key, rawNode, path, depth) => {
    const node = unwrap(rawNode, schema);
    const w = widgetFor(node);
    if (w.kind === 'object') return renderObject(key, node, path, depth);
    if (w.kind === 'array') return renderArray(key, node, path, depth);
    if (!leafVisible(key, node, path)) return null;
    return renderLeaf(key, node, path, getAt(path));
  };

  /** How many editable parameters the schema defines, for the header badge. */
  const fieldCount = useMemo(() => {
    if (!schema) return 0;
    let n = 0;
    const walk = (node, seen) => {
      const props = node.properties || {};
      for (const k of Object.keys(props)) {
        const c = unwrap(props[k], schema);
        const w = widgetFor(c);
        if (w.kind === 'object') {
          if (c.title && seen.has(c.title)) continue;
          if (c.title) seen.add(c.title);
          walk(c, seen);
        } else if (w.kind === 'array') {
          const it = unwrap(c.items || {}, schema);
          if (it.properties) {
            if (it.title && seen.has(it.title)) continue;
            if (it.title) seen.add(it.title);
            walk(it, seen);
          } else n++;
        } else n++;
      }
    };
    walk(schema, new Set());
    return n;
  }, [schema]);

  return (
    <div className="absolute inset-0 flex flex-col bg-black overflow-hidden">
      <div className="p-4 border-b border-white/5 flex gap-3 items-center bg-black/90 backdrop-blur-sm z-20 shrink-0">
        <div className="relative flex-1">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-white/30" />
          <Input
            placeholder="Search parameters and descriptions..."
            value={searchTerm}
            onChange={e => setSearchTerm(e.target.value)}
            className="pl-9 bg-white/5 border-none h-8 text-xs focus-visible:ring-1 focus-visible:ring-teal-500"
          />
        </div>
        {/* Export the exact config under test. Without it, the only way to say
            WHICH scenario produced a result was to describe it in prose, and a
            result whose configuration cannot be handed to someone else is an
            anecdote. */}
        <button
          type="button"
          onClick={() => {
            const blob = new Blob([JSON.stringify(currentConfig, null, 2)],
                                  { type: 'application/json' })
            const url = URL.createObjectURL(blob)
            const a = document.createElement('a')
            a.href = url
            a.download = `${(currentConfig?.name || 'scenario')
              .replace(/[^\w.-]+/g, '-').toLowerCase()}.json`
            a.click()
            URL.revokeObjectURL(url)
          }}
          title="Download this configuration as JSON"
          className="shrink-0 flex items-center gap-1.5 rounded-md border border-white/10 px-2.5 h-8 text-[11px] text-white/60 hover:text-teal-400 hover:border-teal-500/40"
        >
          <Download className="w-3.5 h-3.5" />
          Export
        </button>
        {schema && (
          <span className="text-[10px] text-white/25 font-mono shrink-0">
            {fieldCount} params
          </span>
        )}
        {(reviewCount > 0 || reviewOnly) && (
          <button
            type="button"
            onClick={() => setReviewOnly(v => !v)}
            title={reviewOnly
              ? "Show every parameter again"
              : "Show only the key parameters still on their default value"}
            className={`flex items-center gap-1.5 h-8 px-2.5 rounded-md text-[10px] font-bold uppercase tracking-wide border transition-colors shrink-0 ${
              reviewOnly
                ? 'bg-amber-500/15 border-amber-500/50 text-amber-300'
                : 'bg-transparent border-amber-500/25 text-amber-400/70 hover:border-amber-500/50 hover:text-amber-300'
            }`}
          >
            <AlertTriangle className="w-3 h-3" />
            {!reviewOnly ? `${reviewCount} on default`
              : reviewCount > 0 ? `${reviewCount} left` : 'all set'}
          </button>
        )}
        {dirty && (
          <span
            title="These edits are a draft — the scenario, the canvas and the map still show the old values until you press Sync."
            className="shrink-0 rounded border border-amber-500/40 bg-amber-500/10 px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wide text-amber-400"
          >
            unsynced
          </span>
        )}
        <Button
          size="sm"
          disabled={!currentConfig || syncing}
          title={currentConfig ? "Validate these values and apply them to the scenario"
                               : "Generate or load a scenario first"}
          onClick={() => { void sync(); }}
          className="bg-teal-600 hover:bg-teal-500 h-8 text-xs shrink-0 disabled:opacity-30"
        >
          <Save className="w-3 h-3 mr-2" /> {syncing ? 'Syncing…' : 'Sync'}
        </Button>
      </div>

      <div className="flex-1 overflow-y-auto p-4 space-y-1 custom-scrollbar">
        {syncError && (
          <div className="mb-2 rounded border border-red-500/30 bg-red-950/20 px-3 py-2 text-[11px] leading-relaxed text-red-300">
            <span className="font-bold">Sync rejected:</span> {syncError}
            <p className="mt-1 text-[10px] text-red-300/70">
              The scenario is unchanged — nothing invalid reached it.
            </p>
          </div>
        )}
        {schemaError && (
          <div className="text-[11px] text-amber-400/80 border border-amber-500/20 rounded p-3">
            Could not load the parameter schema from the backend ({schemaError}).
            Start the API and reopen this tab — without the schema this editor
            cannot know which parameters exist or what values they accept.
          </div>
        )}
        {!schema && !schemaError && (
          <p className="text-[11px] text-white/30">Loading parameter schema…</p>
        )}
        {schema && !currentConfig && (
          <p className="text-[10px] text-white/30 border border-white/10 rounded px-3 py-2 mb-2">
            No scenario loaded — every parameter below is showing its default.
            Generate or load a scenario to edit real values.
          </p>
        )}
        {reviewOnly && currentConfig && (
          <div className="text-[10px] leading-relaxed text-amber-200/70 border border-amber-500/25 bg-amber-500/[0.05] rounded px-3 py-2 mb-2">
            {reviewCount > 0 ? (
              <>
                <span className="font-bold text-amber-300">
                  {reviewCount} key parameter{reviewCount !== 1 ? 's' : ''} still on default.
                </span>{' '}
                Your prompt did not pin these down, so the scenario runs on the
                schema&apos;s value for each. Set the ones your test depends on and
                press Sync — or leave them and the defaults stand.
              </>
            ) : (
              <>
                <span className="font-bold text-amber-300">Every key parameter is set.</span>{' '}
                Press Sync to apply these values to the scenario.
              </>
            )}
          </div>
        )}
        {schema && renderNode('root', schema, [], 0)}
      </div>
    </div>
  );
}
