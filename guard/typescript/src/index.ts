// The agent-sandbox guard in TypeScript. The same policy format (guard-policy-v1), hard
// rules, layered decision and decision records (guard-record-v1) as the Python SDK
// (guard/src/guard), checked by the same JSON Schemas and the same test vectors
// (guard/test-vectors). The model adapters and the MCP proxy are Python only.
export { CanonicalError, canonical, digest } from "./canonical.js";
export type { BackendResult, Decision, DecisionBackend, DelegationSource, GuardOptions, Outcome } from "./engine.js";
export { Guard, LOW_CONFIDENCE, failClosed } from "./engine.js";
export type { ActionRules, DecisionLabel, Delegation, Policy } from "./policy.js";
export { PolicyError, parseDelegation, parsePolicy } from "./policy.js";
export type { DecisionRecord, Head, Layer } from "./records.js";
export { DecisionLog, GENESIS, recordHash, verifyChain } from "./records.js";
export type { Check, Fields, Hit } from "./rules.js";
export { Ledger } from "./rules.js";
export { problem } from "./schemas.js";
export { GuardAskUser, GuardBlocked, GuardRefused, guardTool } from "./wrap.js";
