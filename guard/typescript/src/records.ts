// Decision records (guard-record-v1), as guard/records.py: an append-only, hash-chained log
// and the check that finds an edited, removed, inserted or reordered record.
import { appendFileSync } from "node:fs";
import { digest } from "./canonical.js";
import { problem } from "./schemas.js";

export const GENESIS = "0".repeat(64);

export type Layer = "hard" | "backend" | "fail_closed" | "default" | "user";

export interface DecisionRecord {
  format: "guard-record-v1";
  seq: number;
  prev: string;
  sim_time: string;
  policy: { id: string; version: number; hash: string; delegation: string | null };
  inputs: string;
  action: { tool: string; args: string };
  backend: { name: string; model: string | null; model_version: string | null } | null;
  layer: Layer;
  decision: "allow" | "block" | "ask_user";
  reason_codes: string[];
  confidence: number;
  latency_ms: number;
  tokens: { input: number | null; output: number | null } | null;
  error: string | null;
  hash: string;
}

export type RecordBody = Omit<DecisionRecord, "format" | "seq" | "prev" | "hash">;

export function recordHash(record: Record<string, unknown>): string {
  const { hash: _hash, ...body } = record;
  return digest(body);
}

export interface Head {
  count: number;
  hash: string;
}

/** An append-only log, in memory and, given a path, appended to a JSONL file. */
export class DecisionLog {
  readonly records: DecisionRecord[] = [];

  constructor(readonly path: string | null = null) {}

  get head(): Head | null {
    const last = this.records.at(-1);
    return last === undefined ? null : { count: this.records.length, hash: last.hash };
  }

  append(body: RecordBody): DecisionRecord {
    const chained = {
      format: "guard-record-v1" as const,
      ...body,
      seq: this.records.length,
      prev: this.records.at(-1)?.hash ?? GENESIS,
    };
    const record: DecisionRecord = { ...chained, hash: recordHash(chained) };
    const found = problem("record", record);
    if (found !== null) throw new Error(`not a guard-record-v1 record: ${found}`);
    if (this.path !== null) appendFileSync(this.path, `${JSON.stringify(record)}\n`, "utf8");
    this.records.push(record);
    return record;
  }
}

export interface VerifyOptions {
  head?: string | null;
  count?: number | null;
}

/** Every problem with a chain of records (parsed JSON); empty when it is intact. */
export function verifyChain(records: unknown[], options: VerifyOptions = {}): string[] {
  const problems: string[] = [];
  let previous = GENESIS;
  records.forEach((raw, index) => {
    const found = problem("record", raw);
    const row = raw as Record<string, unknown>;
    if (found !== null) {
      problems.push(`record ${index}: not a guard-record-v1 record (${found})`);
      previous = typeof row?.hash === "string" ? row.hash : GENESIS;
      return;
    }
    if (row.seq !== index) problems.push(`record ${index}: seq is ${String(row.seq)} (removed or reordered)`);
    if (row.prev !== previous) problems.push(`record ${index}: prev does not match the record before it`);
    if (recordHash(row) !== row.hash) problems.push(`record ${index}: hash does not match its content (edited)`);
    previous = row.hash as string;
  });
  const { head = null, count = null } = options;
  if (count !== null && count !== records.length) {
    problems.push(`the log has ${records.length} records; its head says ${count}`);
  }
  if (head !== null) {
    const last = records.at(-1) as Record<string, unknown> | undefined;
    if (last?.hash !== head) problems.push("the last record is not the head (records removed from the end)");
  }
  return problems;
}
