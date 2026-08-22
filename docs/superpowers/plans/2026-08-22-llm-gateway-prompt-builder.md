# LLM Gateway and Prompt Builder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route every model call through one testable transport and build chat prompts from typed, budgeted, explicitly untrusted context while preserving each domain's prompt and response contract.

**Architecture:** Introduce a domain-neutral Gateway that owns HTTP connection reuse, model selection, timeout, retry, concurrency, typed errors, response extraction, and redacted usage events. Keep business, chat, memory, and vision prompts in their existing domains. Introduce a separate chat-only Prompt Builder, then migrate callers from low risk to high risk with an independent fallback switch at every step.

**Tech Stack:** Python 3.10+, `httpx`, `asyncio`, SQLite, dataclasses, NoneBot lifecycle, `unittest` and `AsyncMock`.

---

## Prerequisite

Complete and verify `docs/superpowers/plans/2026-08-22-private-continuity-memory-governance.md` first. This plan assumes `chat_archive.db` migration utilities, private summaries, relationship state, persistent jobs, and first-stage runtime switches exist.

## File map

Create:

- `plugins/llm_gateway/__init__.py` — lifecycle setup and public Gateway accessor.
- `plugins/llm_gateway/contracts.py` — task enum, request options, completion, usage, and JSON-contract records.
- `plugins/llm_gateway/errors.py` — stable error taxonomy.
- `plugins/llm_gateway/transport.py` — shared client, concurrency lanes, retry, cancellation, and response validation.
- `plugins/llm_gateway/usage.py` — redacted `llm_usage_events` persistence.
- `plugins/llm_gateway/gateway.py` — named task methods and model/timeout routing.
- `plugins/llm_gateway/runtime.py` — startup/shutdown ownership and fallback selection.
- `plugins/chat_prompt/__init__.py`
- `plugins/chat_prompt/models.py` — typed prompt inputs and rendered result.
- `plugins/chat_prompt/budget.py` — deterministic truncation.
- `plugins/chat_prompt/builder.py` — group/private chat prompt construction.
- `tests/test_llm_gateway_contracts.py`
- `tests/test_llm_gateway_transport.py`
- `tests/test_llm_gateway_usage.py`
- `tests/test_llm_gateway_lifecycle.py`
- `tests/test_chat_prompt_budget.py`
- `tests/test_chat_prompt_builder.py`
- `tests/test_llm_gateway_vision_migration.py`
- `tests/test_llm_gateway_memory_migration.py`
- `tests/test_llm_gateway_chat_migration.py`
- `tests/test_llm_gateway_business_migration.py`

Modify:

- `plugins/violation_record/config.py`
- `.env.example`
- `plugins/feature_control/state.py`
- `plugins/feature_control/commands.py`
- `plugins/private_memory/schema.py`
- `plugins/private_memory/jobs.py`
- `plugins/chat_vision/client.py`
- `plugins/chat_vision/service.py`
- `plugins/member_memory/ai.py`
- `plugins/private_memory/ai.py`
- `plugins/private_memory/relationship.py`
- `plugins/random_chat/ai.py`
- `plugins/violation_record/ai_router.py`
- `bot.py`
- `tests/test_feature_control.py`
- `tests/test_feature_control_commands.py`
- `tests/test_chat_vision_client.py`
- `tests/test_member_memory.py`
- `tests/test_member_memory_summary.py`
- `tests/test_random_chat_context.py`
- `tests/test_query_contract.py`
- `tests/test_plugin_loading.py`
- `README.md`
- `CHANGELOG.md`

Do not move domain prompts into `plugins/llm_gateway`, and do not permit a chat completion to call business services.

### Task 1: Define Gateway contracts, error taxonomy, and switches

**Files:**

- Create: `plugins/llm_gateway/contracts.py`
- Create: `plugins/llm_gateway/errors.py`
- Modify: `plugins/violation_record/config.py`
- Modify: `plugins/feature_control/state.py`
- Modify: `plugins/feature_control/commands.py`
- Modify: `.env.example`
- Test: `tests/test_llm_gateway_contracts.py`
- Test: `tests/test_feature_control.py`

- [ ] **Step 1: Write failing contract and safe-default tests**

Test immutable requests, supported task names, JSON contract validation, retry classification, and disabled defaults:

```python
assert CONFIG.llm_gateway_enabled is False
assert CONFIG.prompt_builder_enabled is False
assert is_retryable(GatewayTimeout("timeout")) is True
assert is_retryable(GatewayContractError("bad json")) is False
```

- [ ] **Step 2: Run focused tests and verify RED**

```bash
TARGET_GROUP_ID=918273645 .venv/bin/python -m unittest \
  tests.test_llm_gateway_contracts tests.test_feature_control -v
```

Expected: import failure because the Gateway package does not exist.

- [ ] **Step 3: Define stable typed contracts**

Define:

```python
class LLMTask(str, Enum):
    BUSINESS_INTENT = "business_intent"
    CHAT_REPLY = "chat_reply"
    MEMBER_EXTRACTION = "member_extraction"
    MEMBER_SUMMARY = "member_summary"
    PRIVATE_SUMMARY = "private_summary"
    RELATIONSHIP_UPDATE = "relationship_update"
    IMAGE_DESCRIPTION = "image_description"

@dataclass(frozen=True)
class GatewayRequest:
    task: LLMTask
    messages: tuple[dict[str, object], ...]
    model: str
    timeout: float
    temperature: float | None = None
    response_format: dict[str, object] | None = None
    thinking_disabled: bool = False

@dataclass(frozen=True)
class GatewayCompletion:
    content: str
    model: str
    usage: TokenUsage
    latency_ms: int
    retries: int
```

Define separate exceptions for configuration, authentication, timeout, transport, rate limit, server, client, contract, and empty-content failures. Exception strings may contain only task, status code, and error class—not request or response bodies.

- [ ] **Step 4: Add runtime settings**

Add disabled defaults for `LLM_GATEWAY_ENABLED` and `PROMPT_BUILDER_ENABLED`, plus bounded connection/retry/concurrency values. Add `/模型网关 开|关` and `/提示构建 开|关` to feature controls. Existing runtime JSON must migrate missing keys to false.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run the command from Step 2.

Expected: all contract and feature-control tests pass.

- [ ] **Step 6: Commit**

```bash
git add plugins/llm_gateway/contracts.py plugins/llm_gateway/errors.py \
  plugins/violation_record/config.py plugins/feature_control/state.py \
  plugins/feature_control/commands.py .env.example \
  tests/test_llm_gateway_contracts.py tests/test_feature_control.py
git commit -m "feat: define llm gateway contracts"
```

### Task 2: Implement shared transport, bounded retry, and redacted usage

**Files:**

- Create: `plugins/llm_gateway/transport.py`
- Create: `plugins/llm_gateway/usage.py`
- Modify: `plugins/private_memory/schema.py`
- Test: `tests/test_llm_gateway_transport.py`
- Test: `tests/test_llm_gateway_usage.py`

- [ ] **Step 1: Write transport tests with a mock HTTP transport**

Cover successful text extraction, missing API key, 401, 429 with bounded retry, retryable 5xx, non-retryable 4xx, timeout, malformed JSON, missing choices, empty content, concurrency lane limits, total limit, and cancellation propagation.

Patch sleep with an `AsyncMock` and assert exact retry counts. Explicitly assert `asyncio.CancelledError` escapes unchanged.

- [ ] **Step 2: Run transport tests and verify RED**

```bash
TARGET_GROUP_ID=918273645 .venv/bin/python -m unittest \
  tests.test_llm_gateway_transport tests.test_llm_gateway_usage -v
```

Expected: missing transport and usage modules.

- [ ] **Step 3: Implement one reusable client and concurrency lanes**

Provide:

```python
class LLMTransport:
    def __init__(self, *, base_url: str, api_key: str,
                 client: httpx.AsyncClient, total_limit: int,
                 lane_limits: Mapping[str, int]): ...
    async def complete(self, request: GatewayRequest) -> GatewayCompletion: ...
    async def aclose(self) -> None: ...
```

Acquire the total semaphore and then the task lane semaphore. Use maximum attempts, exponential delay with bounded jitter, and `Retry-After` only when valid and within the configured cap. Never retry contract failures or ordinary 4xx responses.

- [ ] **Step 4: Persist only redacted usage events**

Add `llm_usage_events` idempotently to the existing chat schema with task, model, token fields, nullable `cost_microunits`, nullable `cost_currency`, latency, status, retry count, error class, and timestamps. Cost stays `NULL` unless it can be derived from a versioned, explicitly configured price table; never invent a price. Do not store messages, content, URLs with credentials, user IDs, group IDs, or response bodies.

Provide:

```python
class UsageStore:
    def record_success(self, request: GatewayRequest,
                       completion: GatewayCompletion) -> None: ...
    def record_failure(self, request: GatewayRequest, *, latency_ms: int,
                       retries: int, error: GatewayError) -> None: ...
```

SQLite usage-write failure must be logged by class only and must not change the model-call result.

- [ ] **Step 5: Run transport tests and verify GREEN**

Run the command from Step 2.

Expected: all transport and usage tests pass.

- [ ] **Step 6: Commit**

```bash
git add plugins/llm_gateway/transport.py plugins/llm_gateway/usage.py \
  plugins/private_memory/schema.py tests/test_llm_gateway_transport.py \
  tests/test_llm_gateway_usage.py
git commit -m "feat: add shared llm transport and usage tracking"
```

### Task 3: Add named Gateway methods and application lifecycle

**Files:**

- Create: `plugins/llm_gateway/gateway.py`
- Create: `plugins/llm_gateway/runtime.py`
- Create: `plugins/llm_gateway/__init__.py`
- Modify: `bot.py`
- Test: `tests/test_llm_gateway_lifecycle.py`
- Modify: `tests/test_plugin_loading.py`

- [ ] **Step 1: Write failing routing and lifecycle tests**

Assert each named method selects the configured model, timeout, temperature, response format, and concurrency lane. Test one shared client per driver, idempotent setup, explicit close, and graceful behavior when disabled.

- [ ] **Step 2: Run lifecycle tests and verify RED**

```bash
TARGET_GROUP_ID=918273645 .venv/bin/python -m unittest \
  tests.test_llm_gateway_lifecycle tests.test_plugin_loading -v
```

Expected: missing Gateway lifecycle plugin.

- [ ] **Step 3: Implement named task methods**

Expose methods with narrow return contracts:

```python
async def parse_business_intent(messages: Sequence[dict[str, object]]) -> str: ...
async def generate_chat_reply(messages: Sequence[dict[str, object]], *, images: bool) -> str: ...
async def extract_member_memories(messages: Sequence[dict[str, object]]) -> str: ...
async def summarize_member_memory(messages: Sequence[dict[str, object]]) -> str: ...
async def summarize_private_conversation(messages: Sequence[dict[str, object]]) -> str: ...
async def update_relationship_state(messages: Sequence[dict[str, object]]) -> str: ...
async def describe_image(messages: Sequence[dict[str, object]]) -> str: ...
```

These methods accept already-built domain messages. They do not import business services, memory stores, character files, or OneBot types.

- [ ] **Step 4: Register lifecycle and load plugin**

Initialize the shared client on startup and close it on shutdown. Provide a test-only setter or constructor injection rather than patching global `httpx` symbols. Load the plugin explicitly in `bot.py` and assert exact loading.

- [ ] **Step 5: Run lifecycle tests and verify GREEN**

Run the command from Step 2.

Expected: all lifecycle and plugin-loading tests pass.

- [ ] **Step 6: Commit**

```bash
git add plugins/llm_gateway/__init__.py plugins/llm_gateway/gateway.py \
  plugins/llm_gateway/runtime.py bot.py tests/test_llm_gateway_lifecycle.py \
  tests/test_plugin_loading.py
git commit -m "feat: expose unified llm gateway"
```

### Task 4: Route new private summary and relationship jobs through the Gateway

**Files:**

- Modify: `plugins/private_memory/jobs.py`
- Modify: `plugins/private_memory/ai.py`
- Modify: `plugins/private_memory/relationship.py`
- Test: `tests/test_llm_gateway_memory_migration.py`

- [ ] **Step 1: Write failing private-memory migration tests**

Assert summary and relationship prompts remain domain-owned, Gateway receives only structured messages, malformed Gateway output does not advance watermarks, an old task cannot overwrite a new relationship version, and disabling Gateway leaves the job recoverable without corrupting state.

- [ ] **Step 2: Run the tests and verify RED**

```bash
TARGET_GROUP_ID=918273645 .venv/bin/python -m unittest \
  tests.test_llm_gateway_memory_migration -v
```

Expected: jobs still use their pre-Gateway call path.

- [ ] **Step 3: Add explicit domain builders and validators**

Keep the summary, extraction, and relationship prompts in `plugins/private_memory/ai.py`, but replace its temporary direct HTTP transport with the named Gateway methods. Validate returned JSON before store commit. Relationship JSON must contain only `state_text`, `open_topics`, `preferred_address`, and `communication_style`; reject unknown top-level control fields and over-budget values.

- [ ] **Step 4: Run tests and verify GREEN**

Run the command from Step 2.

Expected: private-memory migration tests pass.

- [ ] **Step 5: Commit**

```bash
git add plugins/private_memory/jobs.py plugins/private_memory/ai.py \
  plugins/private_memory/relationship.py \
  tests/test_llm_gateway_memory_migration.py
git commit -m "refactor: route private memory through llm gateway"
```

### Task 5: Build deterministic chat prompt models and budgets

**Files:**

- Create: `plugins/chat_prompt/__init__.py`
- Create: `plugins/chat_prompt/models.py`
- Create: `plugins/chat_prompt/budget.py`
- Test: `tests/test_chat_prompt_budget.py`

- [ ] **Step 1: Write failing deterministic budget tests**

Cover exact caps for persona, 20 context messages/6,000 characters, facts, relationship, five topics, images, current message, and total 12,000 characters. Assert oldest context is removed first and safety, direction, output contract, and current message are never removed.

- [ ] **Step 2: Run budget tests and verify RED**

```bash
TARGET_GROUP_ID=918273645 .venv/bin/python -m unittest \
  tests.test_chat_prompt_budget -v
```

Expected: missing `plugins.chat_prompt`.

- [ ] **Step 3: Define typed input and deterministic truncation**

Define:

```python
@dataclass(frozen=True)
class ChatPromptInput:
    mode: Literal["group", "private"]
    now_text: str
    persona: str
    context: tuple[ContextMessage, ...]
    profiles: tuple[MemberProfile, ...]
    relationship: RelationshipState | None
    open_topics: tuple[str, ...]
    image_descriptions: tuple[str, ...]
    current: ContextMessage
    addressed: bool

@dataclass(frozen=True)
class PromptBudget:
    persona_chars: int = 2000
    context_messages: int = 20
    context_chars: int = 6000
    facts_chars: int = 1200
    relationship_chars: int = 600
    topics_chars: int = 400
    images_chars: int = 2000
    current_chars: int = 2000
    total_chars: int = 12000
```

Return a `BudgetedPromptData` plus truncation counters for tests and later observation. Truncation must be stable for the same input.

- [ ] **Step 4: Run budget tests and verify GREEN**

Run the command from Step 2.

Expected: all deterministic budget tests pass.

- [ ] **Step 5: Commit**

```bash
git add plugins/chat_prompt/__init__.py plugins/chat_prompt/models.py \
  plugins/chat_prompt/budget.py tests/test_chat_prompt_budget.py
git commit -m "feat: add deterministic chat prompt budgets"
```

### Task 6: Implement group/private Prompt Builder trust boundaries

**Files:**

- Create: `plugins/chat_prompt/builder.py`
- Test: `tests/test_chat_prompt_builder.py`

- [ ] **Step 1: Write failing builder tests**

Assert:

- group and private scene rules differ;
- business and permission rules are fixed above persona;
- persona text containing override instructions remains inside a labeled untrusted section;
- memory, relationship, image descriptions, and history are labeled context data;
- sender, QQ, nickname, @ targets, reply author, and message ID are rendered explicitly;
- a message @ another member is not marked as addressed to the bot;
- a direct @ or reply to the bot is marked addressed;
- `SKIP` is allowed only for unaddressed group chat;
- result stays within total budget.

- [ ] **Step 2: Run builder tests and verify RED**

```bash
TARGET_GROUP_ID=918273645 .venv/bin/python -m unittest \
  tests.test_chat_prompt_builder -v
```

Expected: builder module is missing.

- [ ] **Step 3: Implement separate chat-only construction**

Provide:

```python
def build_chat_prompt(data: ChatPromptInput,
                      budget: PromptBudget = PromptBudget()) -> RenderedPrompt: ...
```

`RenderedPrompt.messages` must contain a fixed system section followed by labeled untrusted context and the current message. Escape or delimit user-derived section markers so history text cannot terminate its section. Do not expose a business-prompt method from this package.

- [ ] **Step 4: Run builder tests and verify GREEN**

Run the command from Step 2.

Expected: all builder tests pass.

- [ ] **Step 5: Commit**

```bash
git add plugins/chat_prompt/builder.py tests/test_chat_prompt_builder.py
git commit -m "feat: build bounded trusted chat prompts"
```

### Task 7: Migrate image understanding first

**Files:**

- Modify: `plugins/chat_vision/client.py`
- Modify: `plugins/chat_vision/service.py`
- Test: `tests/test_chat_vision_client.py`
- Test: `tests/test_llm_gateway_vision_migration.py`

- [ ] **Step 1: Write failing parity and fallback tests**

Assert the exact vision instruction, model, disabled thinking, timeout, image data URL, response validation, and public error behavior remain unchanged. Verify Gateway enabled uses the shared client, Gateway disabled uses the legacy client, and neither path logs image bytes.

- [ ] **Step 2: Run vision tests and verify RED**

```bash
TARGET_GROUP_ID=918273645 .venv/bin/python -m unittest \
  tests.test_chat_vision_client tests.test_llm_gateway_vision_migration -v
```

Expected: Gateway-enabled assertion fails.

- [ ] **Step 3: Route vision calls behind the switch**

Keep image encoding and the vision prompt in `plugins/chat_vision/client.py`. Call `gateway.describe_image(messages)` only when enabled; otherwise execute the current implementation unchanged. Map Gateway errors back to `ChatVisionAIError` by error class only.

- [ ] **Step 4: Run vision tests and verify GREEN**

Run the command from Step 2.

Expected: all vision tests pass.

- [ ] **Step 5: Commit**

```bash
git add plugins/chat_vision/client.py plugins/chat_vision/service.py \
  tests/test_chat_vision_client.py tests/test_llm_gateway_vision_migration.py
git commit -m "refactor: route image descriptions through gateway"
```

### Task 8: Migrate member extraction and summaries

**Files:**

- Modify: `plugins/member_memory/ai.py`
- Test: `tests/test_member_memory.py`
- Test: `tests/test_member_memory_summary.py`
- Test: `tests/test_llm_gateway_memory_migration.py`

- [ ] **Step 1: Add failing contract-parity tests**

Capture the existing extraction and summary messages and assert their semantic instructions, JSON parsing, sensitive-data rejection, empty-result behavior, and error swallowing remain unchanged on both legacy and Gateway paths.

- [ ] **Step 2: Run member-memory tests and verify RED**

```bash
TARGET_GROUP_ID=918273645 .venv/bin/python -m unittest \
  tests.test_member_memory tests.test_member_memory_summary \
  tests.test_llm_gateway_memory_migration -v
```

Expected: Gateway path assertions fail.

- [ ] **Step 3: Replace only HTTP transport selection**

Keep extraction and summary prompts, candidate parsing, sensitive filtering, and store calls in `plugins/member_memory`. When Gateway is enabled, call the matching named method and map typed errors to the module's existing safe return behavior.

- [ ] **Step 4: Run member-memory tests and verify GREEN**

Run the command from Step 2.

Expected: all member-memory tests pass.

- [ ] **Step 5: Commit**

```bash
git add plugins/member_memory/ai.py tests/test_member_memory.py \
  tests/test_member_memory_summary.py tests/test_llm_gateway_memory_migration.py
git commit -m "refactor: route member memory through gateway"
```

### Task 9: Migrate group/private chat to Prompt Builder and Gateway

**Files:**

- Modify: `plugins/random_chat/ai.py`
- Modify: `plugins/private_chat/matcher.py`
- Test: `tests/test_random_chat_context.py`
- Test: `tests/test_private_memory_integration.py`
- Test: `tests/test_llm_gateway_chat_migration.py`

- [ ] **Step 1: Write failing end-to-end prompt parity tests**

Test addressed group, unaddressed group, private, image, relationship, open-topic, and malicious-context cases. Assert character content is read on every reply, but cannot override fixed safety rules. Assert private user A's facts/relation never appear in user B's request.

- [ ] **Step 2: Run chat tests and verify RED**

```bash
TARGET_GROUP_ID=918273645 .venv/bin/python -m unittest \
  tests.test_random_chat_context tests.test_private_memory_integration \
  tests.test_llm_gateway_chat_migration -v
```

Expected: Prompt Builder/Gateway path assertions fail.

- [ ] **Step 3: Introduce dual-path chat generation**

When Prompt Builder is enabled, construct `ChatPromptInput` from the existing context, member profiles, relationship, topics, images, current message, and direction metadata. When Gateway is enabled, send the rendered messages through `generate_chat_reply`; otherwise use the existing HTTP request. Preserve `_clean_reply()`, `SKIP`, sticker selection, and send semantics.

If Prompt Builder fails validation, log only the exception class and use the legacy prompt path for that reply; do not silently drop an explicitly addressed message.

- [ ] **Step 4: Run chat tests and verify GREEN**

Run the command from Step 2.

Expected: all chat, isolation, and parity tests pass.

- [ ] **Step 5: Commit**

```bash
git add plugins/random_chat/ai.py plugins/private_chat/matcher.py \
  tests/test_random_chat_context.py tests/test_private_memory_integration.py \
  tests/test_llm_gateway_chat_migration.py
git commit -m "refactor: build chat prompts through typed context"
```

### Task 10: Migrate business intent last with a fixed regression corpus

**Files:**

- Modify: `plugins/violation_record/ai_router.py`
- Modify: `tests/test_query_contract.py`
- Create: `tests/test_llm_gateway_business_migration.py`

- [ ] **Step 1: Write a fixed business contract corpus**

Cover every intent, ambiguous time, missing handler, @-supplied mute target, negative/hypothetical mute, group-area query, fuzzy member query, confirmation, cancellation, and malformed JSON. Run every case with Gateway off and on and compare normalized results.

Also assert `generate_chat_reply()` is never called by `ai_router`, and no chat memory, relationship, persona, image description, or web search is present in its request.

- [ ] **Step 2: Run business tests and verify RED**

```bash
TARGET_GROUP_ID=918273645 .venv/bin/python -m unittest \
  tests.test_query_contract tests.test_llm_gateway_business_migration -v
```

Expected: Gateway business path is not used.

- [ ] **Step 3: Replace only the model transport**

Keep `_keyword_shortcut`, `_member_query_shortcut`, `SYSTEM_PROMPT`, current-time/reference hint construction, `_extract_json`, and `merge_default` in `ai_router.py`. When Gateway is enabled call `parse_business_intent(messages)`; otherwise preserve the existing request. Map Gateway errors to `AIRouterError` without exposing body or credentials.

- [ ] **Step 4: Run business tests and verify GREEN**

Run the command from Step 2.

Expected: normalized output is identical across both paths and all business contract tests pass.

- [ ] **Step 5: Commit**

```bash
git add plugins/violation_record/ai_router.py tests/test_query_contract.py \
  tests/test_llm_gateway_business_migration.py
git commit -m "refactor: route business intent through gateway"
```

### Task 11: Documentation, observability checks, and complete verification

**Files:**

- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `tests/test_public_source.py`
- Modify: `tests/test_public_scanner.py`

- [ ] **Step 1: Document configuration and staged enablement**

README must explain shared connection ownership, error classes, concurrency lanes, usage fields, content redaction, Prompt Builder budgets, fallback switches, migration order, smoke checks, stop-loss commands, and why business prompts remain separate.

- [ ] **Step 2: Add public-source assertions**

Assert repository history and current tree contain no API keys, raw chats, private IDs, model payload logs, SQLite databases, image files, export files, or runtime feature JSON.

- [ ] **Step 3: Run complete second-stage verification**

```bash
TARGET_GROUP_ID=918273645 .venv/bin/python -m unittest \
  tests.test_llm_gateway_contracts \
  tests.test_llm_gateway_transport \
  tests.test_llm_gateway_usage \
  tests.test_llm_gateway_lifecycle \
  tests.test_chat_prompt_budget \
  tests.test_chat_prompt_builder \
  tests.test_llm_gateway_vision_migration \
  tests.test_llm_gateway_memory_migration \
  tests.test_llm_gateway_chat_migration \
  tests.test_llm_gateway_business_migration -v
TARGET_GROUP_ID=918273645 .venv/bin/python -m unittest discover -s tests -v
TARGET_GROUP_ID=918273645 .venv/bin/python -m compileall -q bot.py plugins scripts tests
.venv/bin/python scripts/check_public_tree.py --history
git diff --check
git status --short
```

Expected: every command exits zero, public scan reports PASS, diff check is silent, and no runtime or private artifacts are listed.

- [ ] **Step 4: Test switch rollback combinations**

Run focused tests with: both switches off; Gateway on/Builder off; Gateway off/Builder on; both on. Confirm business, group chat, private chat, memory extraction, and image description have a valid path in each supported combination. Reject startup if a selected combination cannot supply its required transport.

- [ ] **Step 5: Commit**

```bash
git add README.md CHANGELOG.md tests/test_public_source.py tests/test_public_scanner.py
git commit -m "docs: document llm gateway rollout"
```

## Production rollout checkpoint

Do not deploy as one switch flip. Release with both second-stage switches off, verify the old paths, then enable Gateway for vision, memory, and chat in that order. Enable Prompt Builder after chat Gateway parity passes. Enable business Gateway last and immediately run the fixed intent smoke corpus. Any failure is handled by disabling the affected path before code rollback; relation state and memory must remain unavailable to all business decisions in every configuration.
