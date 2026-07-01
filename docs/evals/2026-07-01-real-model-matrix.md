# Real Model Coding Gate Matrix, 2026-07-01

This run tested local Ollama models through the native Synode backend after a
full Docker Compose rebuild and platform health check.

Raw reports live under:

- `var/evals/model-matrix/20260702-001412/*/native/*/report.json`
- `var/evals/model-matrix/20260702-001412/*/native/*/report.md`

OpenHands was not run in this matrix because the deployed runtime reported
`OpenHands backend is disabled`. No native fallback was used for OpenHands.

## Scope

Requested model tags were pulled and verified with `ollama show`:

- `qwen3:8b`
- `hermes3:8b`
- `deepseek-coder:6.7b-instruct`
- `opencoder:8b-instruct-q8_0`
- `yi-coder:9b-chat`

The gate used four benchmark tasks:

- `py_cli_date_filter_multifile`
- `sql_refund_revenue_query`
- `sh_retention_filter`
- `tool_argument_repair_probe`

The full task suite remains available, but this matrix used a gate subset
because calibration runs showed `qwen3:8b` could consume about 10 minutes per
task without producing a useful patch. The gate still covers Python, SQL, shell,
repo search/tool use, patch/verify evidence, schema adherence, duplicate tool
calls, and grounded-success checks.

## Summary

| Model | OK | Functional | First action tool call | Schema valid | No duplicate tool calls | Patch+verify | Grounded success |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `qwen3:8b` | 0/4 | 0/4 | 0/4 | 0/4 | 4/4 | 0/4 | 4/4 |
| `hermes3:8b` | 0/4 | 0/4 | 1/4 | 4/4 | 3/4 | 1/4 | 4/4 |
| `deepseek-coder:6.7b-instruct` | 0/4 | 0/4 | 4/4 | 0/4 | 0/4 | 1/4 | 4/4 |
| `opencoder:8b-instruct-q8_0` | 0/4 | 0/4 | 4/4 | 0/4 | 0/4 | 1/4 | 4/4 |
| `yi-coder:9b-chat` | 0/4 | 2/4 | 4/4 | 3/4 | 0/4 | 3/4 | 4/4 |

## Findings

`yi-coder:9b-chat` was the strongest native-loop candidate in this run. It
passed hidden tests on the SQL and fee-rule tasks, kept schema valid on three of
four tasks, and reached patch+verify evidence on three of four tasks. It still
failed the overall Synode gate because duplicate tool calls remained and runtime
status ended as `failed_verification` or timeout.

`deepseek-coder:6.7b-instruct` and `opencoder:8b-instruct-q8_0` reliably started
with tool calls, but both repeatedly violated schema and repeated identical
tool calls. They produced some patches, but did not solve the tasks.

`hermes3:8b` kept JSON schema valid in all four tasks, but often asked for the
operator instead of acting. Its SQL run reached patch+verify but changed a test
instead of fixing the query.

`qwen3:8b` was not usable with the current native loop settings. It timed out on
all gate tasks, failed schema checks, and did not reach patch+verify.

No model produced an ungrounded success. Synode correctly kept failures as
failed, cancelled, waiting-operator, or failed-verification states instead of
claiming success without tool audit, diff, and test evidence.

## Follow-ups

- Tune the native loop to reject repeated identical tool calls earlier and
  compress retry prompts more aggressively.
- Treat repeated `fs_list` with no new information as a stronger failure signal
  in the model-facing observation.
- Investigate why successful hidden tests can still end as `failed_verification`
  for `yi-coder:9b-chat`; this looks like a pipeline/runtime verdict mismatch.
- Re-run the same gate with OpenHands after configuring
  `SYNODE_OPENHANDS_ENABLED=true` and `SYNODE_OPENHANDS_BASE_URL`.
