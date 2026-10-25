// The hard rules, as guard/rules.py: the same order, the same codes, the same decisions.
import type { DecisionLabel, Effective, Hold, ListRule } from "./policy.js";
import { DAYS, PolicyError, checksOf } from "./policy.js";

export const ORDER: Record<DecisionLabel, number> = { allow: 0, ask_user: 1, block: 2 };

export interface Fields {
  amount: number | null;
  counterparty: string | null;
  category: string | null;
  mandate: string | null;
}

export interface Hit {
  code: string;
  decision: DecisionLabel;
}

/** A world's deterministic check: its decision, and whether a request (JSON) breaks it. */
export interface Check {
  decision: Hold;
  check(request: Record<string, unknown>): boolean;
}

const code = (name: string): string => `guard.policy.${name}`;

export function strictest(decisions: DecisionLabel[]): DecisionLabel {
  return decisions.reduce<DecisionLabel>((best, d) => (ORDER[d] > ORDER[best] ? d : best), "allow");
}

interface Entry {
  tool: string;
  amount: number;
  when: Date;
}

/** The amounts a guard allowed, per tool, by simulated UTC day and month. */
export class Ledger {
  readonly entries: Entry[] = [];

  add(tool: string, amount: number, when: Date): void {
    this.entries.push({ tool, amount, when });
  }

  spent(tool: string, when: Date, period: "day" | "month"): number {
    let total = 0;
    for (const entry of this.entries) {
      const then = entry.when;
      let same = then.getUTCFullYear() === when.getUTCFullYear() && then.getUTCMonth() === when.getUTCMonth();
      if (period === "day") same = same && then.getUTCDate() === when.getUTCDate();
      if (entry.tool === tool && same) total += entry.amount;
    }
    return total;
  }
}

function listRule(rule: ListRule, value: string | null, kind: string): Hit[] {
  if (value === null) return [{ code: code(`missing-${kind}`), decision: "ask_user" }];
  if ((rule.block ?? []).includes(value)) return [{ code: code(`${kind}-blocked`), decision: "block" }];
  const hits: Hit[] = [];
  if (rule.allow !== undefined && !rule.allow.includes(value)) {
    hits.push({ code: code(`${kind}-unlisted`), decision: rule.unlisted ?? "ask_user" });
  }
  if ((rule.confirm ?? []).includes(value)) hits.push({ code: code(`${kind}-confirm`), decision: "ask_user" });
  return hits;
}

const pad = (n: number): string => String(n).padStart(2, "0");

function inWindow(rules: Effective, when: Date): Hit[] {
  const day = DAYS[(when.getUTCDay() + 6) % 7];
  const clock = `${pad(when.getUTCHours())}:${pad(when.getUTCMinutes())}`;
  const hits: Hit[] = [];
  for (const rule of rules.rules) {
    const windows = rule.time_windows;
    if (windows === undefined) continue;
    const inside = windows.windows.some(
      (w) => day !== undefined && w.days.includes(day) && w.start <= clock && clock < w.end,
    );
    if (!inside) hits.push({ code: code("outside-time-window"), decision: windows.outside ?? "ask_user" });
  }
  return hits;
}

export interface EvaluateArgs {
  tool: string;
  fields: Fields;
  request: Record<string, unknown>;
  when: Date;
  ledger: Ledger;
  checks: Record<string, Check>;
  defaultDecision: DecisionLabel;
}

/** Every hard rule the action breaks, in a fixed order, each code once (the stricter kept). */
export function evaluate(rules: Effective, args: EvaluateArgs): Hit[] {
  const { tool, fields, request, when, ledger, checks, defaultDecision } = args;
  const hits: Hit[] = [];
  const policy = request.policy as Record<string, unknown> | undefined;
  const allowedTools = policy?.allowed_tools;
  if (Array.isArray(allowedTools) && !allowedTools.includes(tool)) {
    hits.push({ code: code("tool-not-delegated"), decision: "block" });
  }
  if (!rules.covered && defaultDecision !== "allow") {
    hits.push({ code: code("uncovered-tool"), decision: defaultDecision });
  }
  const amount = fields.amount;
  for (const rule of rules.rules) {
    const budget = rule.budget;
    const needsAmount =
      rule.confirm_above !== undefined ||
      (budget !== undefined &&
        [budget.per_action, budget.daily, budget.monthly].some((v) => v !== undefined));
    if (needsAmount && amount === null) {
      hits.push({ code: code("missing-amount"), decision: "ask_user" });
      continue;
    }
    if (amount === null) continue;
    if (budget !== undefined) {
      if (budget.per_action !== undefined && amount > budget.per_action) {
        hits.push({ code: code("over-per-action-budget"), decision: "block" });
      }
      if (budget.daily !== undefined && ledger.spent(tool, when, "day") + amount > budget.daily) {
        hits.push({ code: code("over-daily-budget"), decision: "block" });
      }
      if (budget.monthly !== undefined && ledger.spent(tool, when, "month") + amount > budget.monthly) {
        hits.push({ code: code("over-monthly-budget"), decision: "block" });
      }
    }
    if (rule.confirm_above !== undefined && amount > rule.confirm_above) {
      hits.push({ code: code("confirm-above"), decision: "ask_user" });
    }
  }
  for (const rule of rules.rules) {
    if (rule.counterparties !== undefined) hits.push(...listRule(rule.counterparties, fields.counterparty, "counterparty"));
    if (rule.categories !== undefined) hits.push(...listRule(rule.categories, fields.category, "category"));
    if (rule.mandate !== undefined) {
      if (rule.mandate.required === true && fields.mandate === null) {
        hits.push({ code: code("mandate-required"), decision: "block" });
      } else if (rule.mandate.allow !== undefined && fields.mandate !== null && !rule.mandate.allow.includes(fields.mandate)) {
        hits.push({ code: code("mandate-not-allowed"), decision: "block" });
      }
    }
  }
  hits.push(...inWindow(rules, when));
  for (const id of checksOf(rules.rules)) {
    const found = Object.hasOwn(checks, id) ? checks[id] : undefined;
    if (found === undefined) throw new PolicyError(`the policy names check ${JSON.stringify(id)}, which no world provides`);
    if (found.check(request)) hits.push({ code: id, decision: found.decision === "block" ? "block" : "ask_user" });
  }
  const unique = new Map<string, Hit>();
  for (const hit of hits) {
    const earlier = unique.get(hit.code);
    if (earlier === undefined || ORDER[hit.decision] > ORDER[earlier.decision]) unique.set(hit.code, { ...hit });
  }
  return [...unique.values()];
}
