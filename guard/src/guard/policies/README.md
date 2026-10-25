# Policy packs

One folder per world, one `guard-policy-v1` file per pack (`schemas/policy.schema.json`).
`sandbox-guard policy list` lists them; `sandbox-guard policy check <pack>` validates one and
prints its hash.

| Pack | Tools |
| --- | --- |
| `payments/default` | `pay`, `checkout`, `create_mandate`, `request_refund` |
| `security/default` | `isolate_host`, `reimage_host`, `disable_account`, `reset_credentials`, `block_indicator` |

**Pending: `drug_safety/`.** The drug-safety world does not exist yet: it starts only once a
drug-safety physician has committed as its reviewer. Its pack will cover `submit_case`,
`close_case_invalid` and a seriousness downgrade; no rule for it is written here, and none
will be until that world and its reviewer exist.

Every pack's numbers are invented and wait for that world's reviewer.
