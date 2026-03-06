# Task Checklist

## Done

- [x] Default paper count changed to 3.
- [x] Reading-note structure upgraded with detailed sections:
  - methodology
  - data description
  - empirical conclusions
- [x] LLM runtime config added (provider/base_url/key/model/prompts).
- [x] Frontend settings page updated to include all LLM fields.
- [x] Frontend parameter explanation section added for every configurable key.
- [x] Backend settings API updated to persist new LLM parameters.

## Validation Steps

1. Start backend and frontend.
2. Open settings page and fill LLM endpoint/key/model/prompts.
3. Save settings.
4. Trigger workflow manually.
5. Check run detail notes contain required sections.
