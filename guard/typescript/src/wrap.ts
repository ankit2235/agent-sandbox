// The guard around any tool function, as guard/wrap.py's decorator: allow runs it, block
// throws GuardBlocked, ask_user asks ``confirm`` (true runs it, recorded as the user's
// decision) or throws GuardAskUser. The function never runs unless the guard or the user
// allowed it.
import type { Guard, Outcome } from "./engine.js";
import type { Fields } from "./rules.js";

export class GuardRefused extends Error {
  constructor(readonly outcome: Outcome) {
    super(`${outcome.tool}: ${outcome.decision} (${outcome.reason_codes.join(", ") || "no reason code"})`);
  }
}
export class GuardBlocked extends GuardRefused {}
export class GuardAskUser extends GuardRefused {}

export interface ToolOptions<A extends Record<string, unknown>> {
  tool: string;
  world: string;
  instruction: string;
  /** The simulated time each request and its record carry, as ISO 8601 with an offset. */
  now: () => string;
  fields?: (args: A) => Partial<Fields>;
  state?: (args: A) => Record<string, unknown>;
  policy?: Record<string, unknown>;
  confirm?: (outcome: Outcome) => boolean | Promise<boolean>;
}

/** ``fn`` behind ``guard``: every call is decided first, with its arguments object. */
export function guardTool<A extends Record<string, unknown>, R>(
  guard: Guard,
  fn: (args: A) => R | Promise<R>,
  options: ToolOptions<A>,
): (args: A) => Promise<R> {
  return async (args: A): Promise<R> => {
    const request = {
      world: options.world,
      instruction: options.instruction,
      policy: options.policy ?? {},
      state: options.state?.(args) ?? {},
      recent_steps: [],
      read: [],
      action: { tool: options.tool, args },
      sim_time: options.now(),
    };
    const outcome = await guard.decide(request, options.fields?.(args) ?? null);
    if (outcome.decision === "block") throw new GuardBlocked(outcome);
    if (outcome.decision === "ask_user") {
      if (options.confirm === undefined || !(await options.confirm(outcome))) throw new GuardAskUser(outcome);
      guard.confirmed(outcome, request);
    }
    return fn(args);
  };
}
