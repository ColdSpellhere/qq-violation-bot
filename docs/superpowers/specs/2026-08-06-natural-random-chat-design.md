# Natural Random Chat Design

## Goal

Make random group-chat replies sound like a normal participant instead of an assistant, while preserving the current 3% runtime probability, 30-minute/20-message context window, and plugin boundaries.

## Design

`plugins/random_chat/ai.py` keeps the existing API and request flow. Its system prompt will be replaced with a compact group-chat prompt derived from the MIT-licensed OpenClaw silent-reply pattern and humanizer anti-filler guidance, plus the example-history approach documented by SillyTavern.

The model may return the exact token `SKIP` when there is no natural reply. The response cleaner converts `SKIP`, empty text, and replies beginning with repetitive stock openers into `None`; the matcher already treats `None` as “send nothing.” This keeps delivery logic unchanged and makes rollback a single-file code revert.

## Constraints

- Do not change `RANDOM_CHAT_PROBABILITY` or context retrieval.
- Do not write bot replies into the archive in this iteration.
- Do not modify violation-record, moderation, NapCat, or OneBot code.
- Preserve transport errors as `RandomChatAIError`.

## Verification

Add focused tests for the new prompt, exact `SKIP`, case/whitespace variants, and stock opener filtering. Run the focused tests, then the full test suite, inspect the Git diff, restart only the bot service, and confirm OneBot reconnects without new errors.
