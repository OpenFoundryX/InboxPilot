"""Auto-drafted replies.

Layering, outermost in:

- `store`     — async DB access (get-or-create settings, uploaded files)
- `extract`   — turn an uploaded file into text
- `context`   — assemble the prompt from settings + files, under a char budget
- `generate`  — the one OpenAI call; may decline to draft
- `create`    — the orchestrator both scheduled passes call
- `sweep`     — catch-up pass over recent mail in the selected categories
- `follow_up` — find quiet threads worth nudging

No draft is stored anywhere. The guard against drafting the same email twice is
`gmail.DRAFTED_LABEL`, applied to the source message and excluded by both sweep
queries — see `create._create_and_mark`.
"""
