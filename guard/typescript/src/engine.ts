// The layered decision, failing closed, as guard/engine.py:
//   1. hard rules first, always; a hard block ends the decision and no backend is asked;
//   2. the backend, within the policy's timeout, its answer checked against the shared
//      backend-result schema; a timeout, a throw, an invalid answer or the backend's own
//      failure becomes ask_user, and so does an answer below min_confidence;
//   3. the stricter of the two: nothing a backend answers can make a decision looser.
// The record's time is the request's sim_time; the latency is the only real-clock reading.
import { performance } from "node:perf_hooks";
import { digest } from "./canonical.js";
import type { DecisionLabel, Delegation, Policy } from "./policy.js";
import { PolicyError, effective, parseDelegation } from "./policy.js";
import type { DecisionLog, DecisionRecord, Layer } from "./records.js";
import type { Check, Fields, Hit } from "./rules.js";
import { Ledger, ORDER, evaluate, strictest } from "./rules.js";
import { partProblem, problem } from "./schemas.js";

export const LOW_CONFIDENCE = "guard.low_confidence";

export interface Decision {
  decision: DecisionLabel;
  reason_codes: string[];
  confidence: number;
}

export interface BackendResult {
  decision: Decision;
  backend: string;
  model?: string | null;
  model_version?: string | null;
  probabilities?: Partial<Record<DecisionLabel, number>> | null;
  input_tokens?: number | null;
  output_tokens?: number | null;
  error?: string | null;
}

/** Anything that can decide on an attempted action (as guard.DecisionBackend). */
export interface DecisionBackend {
  name: string;
  decide(request: Record<string, unknown>): Promise<unknown> | unknown;
}

/** The answer for a backend that could not decide: ask the user, never allow. */
export function failClosed(backend: string, error: string): BackendResult {
  const code = error.toLowerCase().replace(/[^a-z0-9_\-]/g, "_") || "error";
  return {
    decision: { decision: "ask_user", reason_codes: [`guard.${code}`], confidence: 0 },
    backend,
    model: null,
    model_version: null,
    error: code,
  };
}

export interface Outcome {
  decision: DecisionLabel;
  reason_codes: string[];
  confidence: number;
  layer: Layer;
  tool: string;
  fields: Fields;
  hits: Hit[];
  backend: BackendResult | null;
  delegation: string | null;
  record: DecisionRecord | null;
}

export type DelegationSource = Delegation | ((request: Record<string, unknown>) => Delegation | null);

export interface GuardOptions {
  backend?: DecisionBackend | null;
  checks?: Record<string, Check>;
  log?: DecisionLog | null;
  delegation?: DelegationSource | null;
  packLimits?: boolean;
  timer?: () => number;
}

const TIMEOUT = Symbol("timeout");

async function ask(backend: DecisionBackend, request: Record<string, unknown>, timeoutS: number): Promise<BackendResult> {
  let timer: NodeJS.Timeout | undefined;
  const expired = new Promise<typeof TIMEOUT>((resolve) => {
    timer = setTimeout(() => resolve(TIMEOUT), timeoutS * 1000);
  });
  let answer: unknown;
  try {
    answer = await Promise.race([Promise.resolve().then(() => backend.decide(request)), expired]);
  } catch {
    return failClosed(backend.name, "backend_error");
  } finally {
    clearTimeout(timer);
  }
  if (answer === TIMEOUT) return failClosed(backend.name, "timeout");
  if (problem("backend-result", answer) !== null) return failClosed(backend.name, "invalid_result");
  return answer as BackendResult;
}

function fieldsOf(given: Partial<Fields> | null | undefined): Fields {
  const raw = given ?? {};
  const found = partProblem("fields", raw);
  if (found !== null) throw new PolicyError(`not policy fields: ${found}`);
  return {
    amount: raw.amount ?? null,
    counterparty: raw.counterparty ?? null,
    category: raw.category ?? null,
    mandate: raw.mandate ?? null,
  };
}

export class Guard {
  readonly ledger = new Ledger();
  readonly backend: DecisionBackend | null;
  readonly checks: Record<string, Check>;
  readonly log: DecisionLog | null;
  readonly delegation: DelegationSource | null;
  readonly packLimits: boolean;
  private readonly timer: () => number;

  constructor(readonly policy: Policy, options: GuardOptions = {}) {
    this.backend = options.backend ?? null;
    this.checks = options.checks ?? {};
    this.log = options.log ?? null;
    this.delegation = options.delegation ?? null;
    this.packLimits = options.packLimits ?? true;
    this.timer = options.timer ?? (() => performance.now());
    this.known(policy.actions, `policy ${policy.id}`);
    if (this.delegation !== null && typeof this.delegation !== "function") {
      this.known(parseDelegation(this.delegation), "the delegation");
    }
  }

  private known(rules: Delegation, label: string): void {
    const named = new Set(Object.values(rules).flatMap((rule) => rule.checks ?? []));
    const missing = [...named].filter((id) => !Object.hasOwn(this.checks, id)).sort();
    if (missing.length > 0) throw new PolicyError(`${label} names checks no world provides: ${missing.join(", ")}`);
  }

  private delegated(request: Record<string, unknown>): [Delegation, string | null] {
    const source = this.delegation;
    const given = typeof source === "function" ? source(request) : source;
    if (given === null || given === undefined || Object.keys(given).length === 0) return [{}, null];
    const rules = parseDelegation(given);
    this.known(rules, "the delegation");
    return [rules, digest(given)];
  }

  /** Decide one action and record it; ``args`` are the raw arguments, when they differ. */
  async decide(
    request: Record<string, unknown>,
    given?: Partial<Fields> | null,
    options: { args?: Record<string, unknown> } = {},
  ): Promise<Outcome> {
    const start = this.timer();
    const found = problem("request", request);
    if (found !== null) throw new PolicyError(`not a decision request: ${found}`);
    const fields = fieldsOf(given);
    const action = request.action as { tool: string; args?: Record<string, unknown> };
    const tool = action.tool;
    const when = new Date(request.sim_time as string);
    const [delegation, delegationHash] = this.delegated(request);
    const rules = effective(this.policy, tool, delegation, this.packLimits);
    const hits = evaluate(rules, {
      tool,
      fields,
      request,
      when,
      ledger: this.ledger,
      checks: this.checks,
      defaultDecision: this.policy.default,
    });
    const hard = strictest(hits.map((hit) => hit.decision));
    const hardCodes = hits.map((hit) => hit.code);
    let decision = hard;
    let codes = hardCodes;
    let confidence = 1;
    const ownHits = hits.filter((hit) => hit.code !== "guard.policy.uncovered-tool");
    let layer: Layer = rules.covered || ownHits.length > 0 ? "hard" : "default";
    let answer: BackendResult | null = null;
    if (hard !== "block" && rules.modelCheck && this.backend !== null) {
      answer = await ask(this.backend, request, this.policy.model_check.timeout_s);
      const said = answer.decision;
      let model: DecisionLabel = said.decision;
      let modelCodes = [...said.reason_codes];
      const failed = answer.error !== null && answer.error !== undefined;
      if (!failed && said.confidence < this.policy.model_check.min_confidence) {
        model = "ask_user";
        modelCodes = [...modelCodes, LOW_CONFIDENCE];
      }
      if (ORDER[model] > ORDER[hard] || hits.length === 0) {
        decision = strictest([hard, model]);
        layer = failed ? "fail_closed" : "backend";
        confidence = said.confidence;
        codes = [...hardCodes, ...(model !== "allow" ? modelCodes : [])];
      }
    }
    if (decision === "allow") {
      codes = [];
      if (fields.amount !== null) this.ledger.add(tool, fields.amount, when);
    }
    const elapsed = this.timer() - start;
    const outcome: Outcome = {
      decision,
      reason_codes: [...new Set(codes)],
      confidence,
      layer,
      tool,
      fields,
      hits,
      backend: answer,
      delegation: delegationHash,
      record: null,
    };
    outcome.record = this.record(outcome, request, options.args ?? action.args ?? {}, elapsed);
    return outcome;
  }

  /** The user confirmed an ask_user: record it, and count its amount as allowed. */
  confirmed(outcome: Outcome, request: Record<string, unknown>): Outcome {
    if (outcome.decision !== "ask_user") throw new Error(`only an ask_user decision can be confirmed, not ${outcome.decision}`);
    if (outcome.fields.amount !== null) this.ledger.add(outcome.tool, outcome.fields.amount, new Date(request.sim_time as string));
    const allowed: Outcome = { ...outcome, decision: "allow", confidence: 1, layer: "user", hits: [], backend: null, record: null };
    const action = request.action as { args?: Record<string, unknown> };
    allowed.record = this.record(allowed, request, action.args ?? {}, 0);
    return allowed;
  }

  private record(outcome: Outcome, request: Record<string, unknown>, args: Record<string, unknown>, elapsedMs: number): DecisionRecord | null {
    if (this.log === null) return null;
    const answer = outcome.backend;
    const tokens =
      answer !== null && (answer.input_tokens != null || answer.output_tokens != null)
        ? { input: answer.input_tokens ?? null, output: answer.output_tokens ?? null }
        : null;
    return this.log.append({
      sim_time: request.sim_time as string,
      policy: { id: this.policy.id, version: this.policy.version, hash: this.policy.hash, delegation: outcome.delegation },
      inputs: digest({ request, fields: outcome.fields }),
      action: { tool: outcome.tool, args: digest(args) },
      backend: answer === null ? null : { name: answer.backend, model: answer.model ?? null, model_version: answer.model_version ?? null },
      layer: outcome.layer,
      decision: outcome.decision,
      reason_codes: outcome.reason_codes,
      confidence: outcome.confidence,
      latency_ms: Math.round(elapsedMs * 1000) / 1000,
      tokens,
      error: answer?.error ?? null,
    });
  }
}
