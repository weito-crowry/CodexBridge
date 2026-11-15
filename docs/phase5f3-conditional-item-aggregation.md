# Phase 5F-3 conditional item aggregation

`poc/ows/fixtures/conditional_items_codex_workflow.yaml` adds one fixed
three-job live template. Submit it through the existing workflow API with
`max_live_codex_jobs: 3` and an input shaped like:

```json
{
  "items": [
    {"id": "gamma", "enabled": true, "prompt": "Reply with gamma"},
    {"id": "beta", "enabled": false, "prompt": "Reply with beta"},
    {"id": "alpha", "enabled": true, "prompt": "Reply with alpha"}
  ]
}
```

The workflow visits items in input order. It creates one Codex job for each
enabled item, one at a time, and returns a skipped row for each disabled item.
An input has at most four items and at most three enabled items. IDs are unique
ASCII stable identifiers up to 64 characters. Every item requires a prompt,
including disabled items; prompts use the existing Codex prompt validation.

The result is `{"results": [...], "summary": {"total": 3, "succeeded": 2,
"skipped": 1}}`. Each result row contains exactly `id`, `status`, `job_id`,
`request_key`, and `final_text`. Skipped rows use nulls for the last three
fields. The service checks enabled rows against the Codex job ledger receipts
and recomputes the summary before reporting success.

Static validation accepts only this item template structure, its fixed enabled
condition, prompt projection, row projection, and aggregate expression. The
existing issues template remains unchanged and continues sorting by issue
number.
