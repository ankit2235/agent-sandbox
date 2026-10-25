// Policies (guard-policy-v1), as guard/policy.py: checked against the shared schema, hashed
// over their canonical JSON, and joined with a delegation so the stricter always wins.
import { digest } from "./canonical.js";
import { partProblem, problem } from "./schemas.js";

export type DecisionLabel = "allow" | "block" | "ask_user";
export type Hold = "block" | "ask_user";
export const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"] as const;

export interface ListRule {
  allow?: string[];
  block?: string[];
  confirm?: string[];
  unlisted?: Hold;
}

export interface ActionRules {
  budget?: { per_action?: number; daily?: number; monthly?: number };
  confirm_above?: number;
  counterparties?: ListRule;
  categories?: ListRule;
  mandate?: { required?: boolean; allow?: string[] };
  time_windows?: {
    windows: { days: (typeof DAYS)[number][]; start: string; end: string }[];
    outside?: Hold;
  };
  checks?: string[];
  model_check?: boolean;
}

export interface Policy {
  format: "guard-policy-v1";
  id: string;
  version: number;
  world: string | null;
  description: string;
  default: DecisionLabel;
  model_check: { min_confidence: number; timeout_s: number };
  actions: Record<string, ActionRules>;
  hash: string;
}

export type Delegation = Record<string, ActionRules>;

export class PolicyError extends Error {}

function windows(rules: Record<string, ActionRules>, label: string): void {
  for (const [tool, rule] of Object.entries(rules)) {
    for (const window of rule.time_windows?.windows ?? []) {
      if (window.start >= window.end) {
        throw new PolicyError(`${label} ${tool}: a window must start before it ends (${window.start})`);
      }
    }
  }
}

/** A policy from parsed JSON, checked against the shared schema. */
export function parsePolicy(raw: unknown): Policy {
  const found = problem("policy", raw);
  if (found !== null) throw new PolicyError(`not a guard-policy-v1 policy: ${found}`);
  const given = raw as Omit<Policy, "hash" | "model_check"> & { model_check?: Partial<Policy["model_check"]> };
  windows(given.actions, "policy");
  return {
    format: given.format,
    id: given.id,
    version: given.version,
    world: given.world ?? null,
    description: given.description ?? "",
    default: given.default ?? "ask_user",
    model_check: {
      min_confidence: given.model_check?.min_confidence ?? 0.5,
      timeout_s: given.model_check?.timeout_s ?? 10,
    },
    actions: given.actions,
    hash: digest(raw),
  };
}

/** A delegation (per-tool rules) checked against the shared schema. */
export function parseDelegation(raw: unknown): Delegation {
  const found = partProblem("delegation", raw);
  if (found !== null) throw new PolicyError(`not a delegation: ${found}`);
  const rules = raw as Delegation;
  windows(rules, "delegation");
  return rules;
}

export interface Effective {
  covered: boolean;
  rules: ActionRules[];
  modelCheck: boolean;
}

/** The rules for one tool: every rule of the policy and the delegation applies. */
export function effective(policy: Policy, tool: string, delegation: Delegation, packLimits: boolean): Effective {
  const own = Object.hasOwn(policy.actions, tool) ? policy.actions[tool] : undefined;
  const rules: ActionRules[] = [];
  if (own !== undefined) {
    if (packLimits) rules.push(own);
    else {
      const { budget: _budget, confirm_above: _confirm, ...rest } = own;
      rules.push(rest);
    }
  }
  const delegated = Object.hasOwn(delegation, tool) ? delegation[tool] : undefined;
  if (delegated !== undefined) rules.push(delegated);
  return { covered: own !== undefined, rules, modelCheck: own !== undefined && (own.model_check ?? true) };
}

export function checksOf(rules: ActionRules[]): string[] {
  return [...new Set(rules.flatMap((rule) => rule.checks ?? []))];
}
