# Chat Vision Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Understand every new image posted in an enabled chat group, keep its original for seven days and its factual description permanently, and use raw images for image-directed replies without touching violation evidence.

**Architecture:** A new `plugins/chat_vision` package owns live-event ingestion, a dedicated image root, persistent asset rows in `chat_archive.db`, the experimental DeepSeek vision client, recovery, and cleanup. The existing group router and random-chat AI consume this package through narrow read APIs; business and evidence modules remain unchanged.

**Tech Stack:** Python 3.10, NoneBot 2, OneBot V11, SQLite, httpx, unittest

---

## File structure

- Create `plugins/chat_vision/__init__.py`: plugin registration and lifecycle setup.
- Create `plugins/chat_vision/store.py`: schema, idempotent state transitions, description/context reads, and expired-row selection.
- Create `plugins/chat_vision/download.py`: independent SSRF-safe chat-image downloader and bounded file writer.
- Create `plugins/chat_vision/client.py`: DeepSeek vision description and OpenAI-compatible image payload helpers.
- Create `plugins/chat_vision/service.py`: live ingestion, pending recovery, and seven-day cleanup orchestration.
- Create `plugins/chat_vision/matcher.py`: enabled-group live image matcher at priority 2.
- Create `plugins/chat_vision/lifecycle.py`: startup recovery/cleanup and one daily cleanup task.
- Modify `plugins/violation_record/config.py`: vision settings and independent paths.
- Modify `plugins/chat_archive/db.py`: attach permanent image descriptions to the existing 30-minute/20-message context.
- Modify `plugins/random_chat/ai.py`: accept raw image inputs and select the vision model only for image-bearing replies.
- Modify `plugins/random_chat/matcher.py`: load current/referenced image assets and descriptions.
- Modify `plugins/group_router/matcher.py`: make pure-image messages eligible and preserve addressed-image mandatory replies.
- Modify `bot.py`: explicitly load the new plugin.
- Modify `.env.example`, `README.md`, and `CHANGELOG.md`: configuration and operations guidance.
- Create focused tests under `tests/test_chat_vision_*.py`; extend routing, archive, AI, plugin-loading, and public-source tests.

### Task 1: Configuration and persistent asset store

**Files:**
- Modify: `plugins/violation_record/config.py`
- Create: `plugins/chat_vision/store.py`
- Create: `tests/test_chat_vision_store.py`
- Modify: `tests/test_feature_control.py`

- [ ] **Step 1: Write failing configuration and store tests**

Create tests that use a temporary SQLite file and assert the public API below:

```python
class ChatVisionStoreTests(unittest.TestCase):
    def test_create_is_idempotent_and_preserves_all_ordinals(self):
        store = ChatVisionStore(self.db_path)
        first = store.ensure_pending(100, "m1", 1, "https://cdn.example/1.jpg", 1000)
        same = store.ensure_pending(100, "m1", 1, "https://cdn.example/1.jpg", 1000)
        second = store.ensure_pending(100, "m1", 2, "https://cdn.example/2.jpg", 1000)
        self.assertEqual(first.id, same.id)
        self.assertEqual([1, 2], [item.ordinal for item in store.for_message(100, "m1")])

    def test_ready_description_survives_file_deletion(self):
        store = ChatVisionStore(self.db_path)
        asset = store.ensure_pending(100, "m1", 1, "https://cdn.example/1.jpg", 1000)
        store.mark_downloaded(
            asset.id, "m1-1.jpg", "image/jpeg", 12, "abc",
            "2026-08-28 00:00:00",
        )
        store.mark_ready(asset.id, "一朵花")
        store.mark_deleted(asset.id, "2026-08-29 00:00:00")
        saved = store.for_message(100, "m1")[0]
        self.assertEqual("一朵花", saved.description)
        self.assertIsNone(saved.relative_path)
```

Extend the configuration subprocess test to assert:

```python
assert CONFIG.chat_vision_enabled is False
assert CONFIG.chat_vision_model == "deepseek-v4-flash-vision-exp"
assert CONFIG.chat_vision_retention_days == 7
assert CONFIG.chat_vision_max_bytes == 10 * 1024 * 1024
assert CONFIG.chat_vision_root.name == "images"
assert CONFIG.chat_vision_root.parent.name == "chat_vision"
assert CONFIG.chat_vision_root != CONFIG.evidence_root
```

- [ ] **Step 2: Run RED**

Run: `TARGET_GROUP_ID="$(.venv/bin/python -c 'import secrets; print(secrets.randbelow(900000000) + 100000000)')" .venv/bin/python -m unittest tests.test_chat_vision_store tests.test_feature_control -v`

Expected: imports or attribute assertions fail because the store and configuration do not exist.

- [ ] **Step 3: Add configuration fields**

Add this validator before `AppConfig`:

```python
def _chat_vision_root_env() -> Path:
    allowed_root = (DATA_DIR / "chat_vision").resolve()
    raw = Path(os.getenv("CHAT_VISION_IMAGE_ROOT", "data/chat_vision/images"))
    configured = (raw if raw.is_absolute() else BASE_DIR / raw).resolve()
    if not configured.is_relative_to(allowed_root):
        raise RuntimeError("CHAT_VISION_IMAGE_ROOT must stay under data/chat_vision")
    return configured
```

Add these fields to `AppConfig`:

```python
chat_vision_enabled: bool = _bool_env("CHAT_VISION_ENABLED", False)
chat_vision_model: str = os.getenv(
    "CHAT_VISION_MODEL", "deepseek-v4-flash-vision-exp"
).strip()
chat_vision_root: Path = _chat_vision_root_env()
chat_vision_retention_days: int = max(1, _int_env("CHAT_VISION_RETENTION_DAYS", 7))
chat_vision_max_bytes: int = max(1, _int_env("CHAT_VISION_MAX_BYTES", 10 * 1024 * 1024))
chat_vision_timeout: int = max(1, _int_env("CHAT_VISION_TIMEOUT", 60))
chat_vision_max_retries: int = max(1, _int_env("CHAT_VISION_MAX_RETRIES", 3))
```

- [ ] **Step 4: Implement the store contract**

Define this immutable row model and store interface in `plugins/chat_vision/store.py`:

```python
@dataclass(frozen=True)
class ChatImageAsset:
    id: int
    group_id: int
    message_id: str
    ordinal: int
    source_url: str
    event_time: int
    status: str
    attempts: int
    relative_path: str | None
    mime_type: str | None
    byte_size: int | None
    sha256: str | None
    description: str | None
    expires_at: str | None
    deleted_at: str | None


```

Implement `ChatVisionStore` with these exact public methods: `ensure_pending(group_id, message_id, ordinal, source_url, event_time)`, `claim(asset_id, max_retries)`, `mark_downloaded(asset_id, relative_path, mime_type, byte_size, sha256, expires_at)`, `mark_ready(asset_id, description)`, `mark_failed(asset_id, error_type)`, `mark_deleted(asset_id, deleted_at)`, `for_message(group_id, message_id)`, `claimable(max_retries)`, and `expired(now_text)`. Every row-returning method returns `ChatImageAsset` values rather than raw SQLite tuples.

Use `UNIQUE(group_id,message_id,ordinal)`, statuses `pending/processing/ready/failed`, `BEGIN IMMEDIATE` for claims, and reset interrupted `processing` rows to `pending` during schema initialization. `mark_downloaded` persists the file metadata and explicit expiry before the model call. `mark_ready` adds the description without replacing file metadata. `mark_failed` preserves any already-downloaded path so an addressed reply and the seven-day cleanup can still use it.

- [ ] **Step 5: Run GREEN and commit**

Run: `TARGET_GROUP_ID="$(.venv/bin/python -c 'import secrets; print(secrets.randbelow(900000000) + 100000000)')" .venv/bin/python -m unittest tests.test_chat_vision_store tests.test_feature_control -v`

Expected: all selected tests pass.

```bash
git add plugins/violation_record/config.py plugins/chat_vision/store.py tests/test_chat_vision_store.py tests/test_feature_control.py
git commit -m "feat: persist chat vision assets"
```

### Task 2: Secure download and evidence-proof cleanup

**Files:**
- Create: `plugins/chat_vision/download.py`
- Create: `plugins/chat_vision/service.py`
- Create: `tests/test_chat_vision_files.py`

- [ ] **Step 1: Write failing file-boundary tests**

Cover valid JPEG storage, oversized data, invalid signatures, private-address rejection, symlink refusal, and this evidence sentinel case:

```python
async def test_cleanup_never_touches_evidence_sibling(self):
    chat_root = self.root / "data" / "chat_vision" / "images"
    evidence_root = self.root / "evidence"
    evidence_root.mkdir(parents=True)
    sentinel = evidence_root / "keep.jpg"
    sentinel.write_bytes(b"evidence")
    asset_path = chat_root / "100" / "2026-08-21" / "m1-1.jpg"
    asset_path.parent.mkdir(parents=True)
    asset_path.write_bytes(b"chat")

    await cleanup_expired(self.store, chat_root, now_text="2026-08-29 00:00:00")

    self.assertEqual(b"evidence", sentinel.read_bytes())
```

- [ ] **Step 2: Run RED**

Run: `TARGET_GROUP_ID="$(.venv/bin/python -c 'import secrets; print(secrets.randbelow(900000000) + 100000000)')" .venv/bin/python -m unittest tests.test_chat_vision_files -v`

Expected: import fails because the downloader and cleanup service are absent.

- [ ] **Step 3: Implement independent download and controlled writes**

Define an immutable `DownloadedChatImage` with `content: bytes`, `mime_type: str`, and `extension: str`. Expose `download_chat_image(url, *, client, max_bytes, resolver=_default_resolver) -> DownloadedChatImage` and `write_chat_image(root, *, group_id, event_time, message_id, ordinal, image) -> tuple[str, str]`.

Reject non-HTTP(S), redirects, private/loopback/link-local/reserved resolutions, unsupported MIME types, mismatched signatures, and content above the byte limit. Write through a temporary file in the destination directory, `chmod(0o600)`, atomically replace the final path, and return `(relative_path, sha256)`.

Implement `cleanup_expired` so it joins only store-provided relative paths beneath `root`, rejects absolute paths and symlinks, verifies `candidate.resolve().is_relative_to(root.resolve())`, unlinks only regular files, and then calls `mark_deleted`. It must never accept `CONFIG.evidence_root` as an input or discover sibling directories.

- [ ] **Step 4: Run GREEN and commit**

Run: `TARGET_GROUP_ID="$(.venv/bin/python -c 'import secrets; print(secrets.randbelow(900000000) + 100000000)')" .venv/bin/python -m unittest tests.test_chat_vision_files tests.test_evidence_capture tests.test_evidence_store -v`

Expected: chat file tests and all existing evidence tests pass.

```bash
git add plugins/chat_vision/download.py plugins/chat_vision/service.py tests/test_chat_vision_files.py
git commit -m "feat: isolate temporary chat images"
```

### Task 3: DeepSeek vision client

**Files:**
- Create: `plugins/chat_vision/client.py`
- Create: `tests/test_chat_vision_client.py`

- [ ] **Step 1: Write failing payload tests**

Use a fake `httpx.AsyncClient` and assert:

```python
description = await describe_image(
    b"jpeg-bytes", "image/jpeg",
    base_url="https://api.deepseek.com",
    api_key="secret",
    model="deepseek-v4-flash-vision-exp",
    timeout=60,
)
self.assertEqual("一名粉发小精灵在飞。", description)
self.assertEqual("disabled", payload["thinking"]["type"])
self.assertEqual("deepseek-v4-flash-vision-exp", payload["model"])
self.assertEqual("image_url", payload["messages"][0]["content"][1]["type"])
self.assertTrue(payload["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
```

Also assert empty content, non-200 responses, malformed JSON, and missing API key raise `ChatVisionAIError` without including the key or Data URL in the exception text.

- [ ] **Step 2: Run RED**

Run: `TARGET_GROUP_ID="$(.venv/bin/python -c 'import secrets; print(secrets.randbelow(900000000) + 100000000)')" .venv/bin/python -m unittest tests.test_chat_vision_client -v`

Expected: import fails because the client is absent.

- [ ] **Step 3: Implement the client**

Expose:

```python
class ChatVisionAIError(RuntimeError):
    pass

def image_data_url(content: bytes, mime_type: str) -> str:
    return f"data:{mime_type};base64,{base64.b64encode(content).decode('ascii')}"

```

Add `describe_image(content, mime_type, *, base_url, api_key, model, timeout) -> str` as the async client entry point.

POST to `{base_url}/v1/chat/completions` with `thinking.type=disabled`. The text instruction must request concise factual Chinese, visible text/OCR, and no invented identity or off-image facts. Convert transport, HTTP, schema, and empty-output failures into `ChatVisionAIError(type(exc).__name__)`.

- [ ] **Step 4: Run GREEN and commit**

Run: `TARGET_GROUP_ID="$(.venv/bin/python -c 'import secrets; print(secrets.randbelow(900000000) + 100000000)')" .venv/bin/python -m unittest tests.test_chat_vision_client -v`

Expected: all client tests pass.

```bash
git add plugins/chat_vision/client.py tests/test_chat_vision_client.py
git commit -m "feat: call DeepSeek vision model"
```

### Task 4: Live-only ingestion, recovery, and lifecycle

**Files:**
- Modify: `plugins/chat_vision/service.py`
- Create: `plugins/chat_vision/matcher.py`
- Create: `plugins/chat_vision/lifecycle.py`
- Create: `plugins/chat_vision/__init__.py`
- Modify: `bot.py`
- Create: `tests/test_chat_vision_ingestion.py`
- Modify: `tests/test_plugin_loading.py`

- [ ] **Step 1: Write failing live-ingestion tests**

Assert the matcher accepts only `GroupMessageEvent` objects for which `CONFIG.chat_vision_enabled` and `FEATURES.group_chat_allowed(group_id)` are true. Test two image segments create ordinals 1 and 2, repeated handler execution is idempotent, and an event without images makes no store rows.

Test startup behavior with a fake store:

```python
await recover_pending(store, processor, max_retries=3)
store.claimable.assert_called_once_with(3)
archive_scan.assert_not_called()
```

The implementation must not define or call any function that scans `chat_messages` to create vision work.

- [ ] **Step 2: Run RED**

Run: `TARGET_GROUP_ID="$(.venv/bin/python -c 'import secrets; print(secrets.randbelow(900000000) + 100000000)')" .venv/bin/python -m unittest tests.test_chat_vision_ingestion tests.test_plugin_loading -v`

Expected: plugin imports and loading assertions fail.

- [ ] **Step 3: Implement one idempotent processor**

Expose this service entry point:

```python
async def process_image_event(event: GroupMessageEvent) -> list[ChatImageAsset]:
    """Create/claim/process every image segment from this live event."""
```

For each segment URL, call `ensure_pending` and atomically `claim`. If no valid stored file exists, download and write it, calculate expiry from event time plus `CONFIG.chat_vision_retention_days`, and call `mark_downloaded` before invoking the model. Then describe and `mark_ready`. A retry with a valid stored file skips the download and retries only description. On a per-image exception, call `mark_failed` with only the exception type and continue with remaining images.

Register a priority-2, `block=False` matcher whose rule checks the existing runtime controller. The handler calls `process_image_event` and never sends a message.

- [ ] **Step 4: Implement lifecycle without backfill**

`setup_lifecycle()` registers one startup callback and one shutdown callback. Startup initializes the store, resets interrupted claims, runs `recover_pending` only over `store.claimable`, runs cleanup once, and starts one daily cleanup task. Shutdown cancels and awaits that task. Do not query `chat_messages` from lifecycle code.

When `CHAT_VISION_ENABLED=false`, startup still initializes schema and performs safe expiry cleanup, but does not recover or submit model work. `plugins/chat_vision/__init__.py` must contain exactly the package-level wiring needed to import `matcher` and call `setup_lifecycle()` once.

Load `plugins.chat_vision` in `bot.py` before `plugins.group_router`, and assert the exact module is loaded in `tests/test_plugin_loading.py`.

- [ ] **Step 5: Run GREEN and commit**

Run: `TARGET_GROUP_ID="$(.venv/bin/python -c 'import secrets; print(secrets.randbelow(900000000) + 100000000)')" .venv/bin/python -m unittest tests.test_chat_vision_ingestion tests.test_plugin_loading -v`

Expected: live gating, all-image ingestion, idempotency, recovery, single lifecycle task, and plugin loading tests pass.

```bash
git add plugins/chat_vision plugins/violation_record/config.py bot.py tests/test_chat_vision_ingestion.py tests/test_plugin_loading.py
git commit -m "feat: ingest live chat images"
```

### Task 5: Permanent descriptions in recent chat context

**Files:**
- Modify: `plugins/chat_archive/db.py`
- Modify: `plugins/random_chat/ai.py`
- Modify: `tests/test_chat_archive.py`
- Modify: `tests/test_random_chat_context.py`

- [ ] **Step 1: Write failing context tests**

Insert one image-only archived message and two ready `chat_image_assets` descriptions. Assert `recent_text_context` includes the message even though `plaintext` is empty and returns:

```python
ContextMessage(
    "小花", "[图片]", message_id="m1", user_id="7",
    image_descriptions=("一朵白花", "一只绿色小虫"),
)
```

Assert a deleted original still contributes its description, while a failed asset with no description does not.

- [ ] **Step 2: Run RED**

Run: `TARGET_GROUP_ID="$(.venv/bin/python -c 'import secrets; print(secrets.randbelow(900000000) + 100000000)')" .venv/bin/python -m unittest tests.test_chat_archive tests.test_random_chat_context -v`

Expected: `ContextMessage` lacks `image_descriptions` and image-only rows are filtered out.

- [ ] **Step 3: Extend context reads**

Add `image_descriptions: tuple[str, ...] = ()` to `ContextMessage`. Detect `chat_image_assets` through `sqlite_master`: if the table is absent, execute the existing text-only query unchanged; if present, accept a non-command text row or an `EXISTS` ready description and fetch descriptions by `(group_id,message_id)` ordered by ordinal. This preserves text chat even when vision is disabled or schema initialization has not run. Use `[图片]` when plaintext is empty and append descriptions in `_format_turn` as:

```python
image_context = "".join(f"\n[图片理解：{item}]" for item in message.image_descriptions)
```

Keep the existing 30-minute filter, 20-message limit, sender identity, mentions, and reply direction unchanged.

- [ ] **Step 4: Run GREEN and commit**

Run: `TARGET_GROUP_ID="$(.venv/bin/python -c 'import secrets; print(secrets.randbelow(900000000) + 100000000)')" .venv/bin/python -m unittest tests.test_chat_archive tests.test_random_chat_context tests.test_random_chat -v`

Expected: archive/context tests pass and text-only prompts remain unchanged except for the new optional field.

```bash
git add plugins/chat_archive/db.py plugins/random_chat/ai.py tests/test_chat_archive.py tests/test_random_chat_context.py tests/test_random_chat.py
git commit -m "feat: remember chat image descriptions"
```

### Task 6: Raw-image replies and pure-image probability routing

**Files:**
- Modify: `plugins/random_chat/ai.py`
- Modify: `plugins/random_chat/matcher.py`
- Modify: `plugins/group_router/matcher.py`
- Modify: `tests/test_random_chat.py`
- Modify: `tests/test_random_chat_context.py`
- Modify: `tests/test_group_router.py`

- [ ] **Step 1: Write failing routing and payload tests**

Add tests proving:

- addressed image-only messages always call `send_random_reply`;
- non-addressed image-only messages call it only when `should_reply` is true;
- an image plus text follows the same probability rule as ordinary text;
- current images and non-expired replied-message images are passed as raw `VisionImage` values;
- expired originals are absent while permanent descriptions remain in context;
- image-bearing AI requests use `CONFIG.chat_vision_model`, `thinking.disabled`, and OpenAI `image_url` blocks;
- text-only AI requests still use `CONFIG.ai_model` and the existing string user content.

- [ ] **Step 2: Run RED**

Run: `TARGET_GROUP_ID="$(.venv/bin/python -c 'import secrets; print(secrets.randbelow(900000000) + 100000000)')" .venv/bin/python -m unittest tests.test_group_router tests.test_random_chat tests.test_random_chat_context -v`

Expected: pure images are ignored and `generate_reply` has no image parameter.

- [ ] **Step 3: Add raw image value and AI payload support**

Define in `plugins/chat_vision/client.py`:

```python
@dataclass(frozen=True)
class VisionImage:
    content: bytes
    mime_type: str
    message_id: str
    ordinal: int
```

Extend the existing `generate_reply` signature with `images: Sequence[VisionImage] = ()`. For images, set model to `CONFIG.chat_vision_model`, add `thinking={"type": "disabled"}`, omit `temperature`, and make the user content an array containing the existing formatted prompt as one text block followed by all image blocks. For no images, preserve the current payload byte-for-byte in behavior.

- [ ] **Step 4: Load current and quoted raw assets**

Add store helpers that return only existing, non-symlink regular files still under `CONFIG.chat_vision_root` and not marked deleted. In `send_random_reply`, load all current-message assets plus all assets belonging to `reply_message_id`; deduplicate by asset ID. Use descriptions even when files no longer exist.

Use `text.strip() or "[图片]"` for the current context text. A failed direct vision reply returns `False` and must not emit a pretend image interpretation.

- [ ] **Step 5: Make pure images candidates**

Add:

```python
def has_image(event: GroupMessageEvent) -> bool:
    return any(segment.type == "image" for segment in event.message)
```

In the group router, retain business-first behavior. Addressed allowed-group messages call `send_random_reply` even when text is empty. For unaddressed messages, call it when either eligible text exists or `has_image(event)` is true, gated by the existing probability exactly once.

- [ ] **Step 6: Run GREEN and commit**

Run: `TARGET_GROUP_ID="$(.venv/bin/python -c 'import secrets; print(secrets.randbelow(900000000) + 100000000)')" .venv/bin/python -m unittest tests.test_group_router tests.test_random_chat tests.test_random_chat_context tests.test_chat_vision_client -v`

Expected: all pure-image, addressed-image, raw-reference, expiry, and existing text routing tests pass.

```bash
git add plugins/chat_vision/client.py plugins/random_chat/ai.py plugins/random_chat/matcher.py plugins/group_router/matcher.py tests/test_group_router.py tests/test_random_chat.py tests/test_random_chat_context.py tests/test_chat_vision_client.py
git commit -m "feat: reply with chat image understanding"
```

### Task 7: Documentation, full verification, and production rollout

**Files:**
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `tests/test_public_source.py`

- [ ] **Step 1: Document configuration and guarantees**

Add synthetic values to `.env.example`:

```dotenv
CHAT_VISION_ENABLED=false
CHAT_VISION_MODEL=deepseek-v4-flash-vision-exp
CHAT_VISION_IMAGE_ROOT=data/chat_vision/images
CHAT_VISION_RETENTION_DAYS=7
CHAT_VISION_MAX_BYTES=10485760
CHAT_VISION_TIMEOUT=60
CHAT_VISION_MAX_RETRIES=3
```

Document live-only ingestion, every-image description, seven-day original retention, permanent descriptions, pure-image probability, addressed-image mandatory replies, same API key, and the `data/chat_vision/images/` versus `evidence/` hard boundary. Do not document or commit the real API key, group IDs, CDN URLs, or image data.

- [ ] **Step 2: Run static and focused verification**

Run:

```bash
.venv/bin/python -m compileall -q plugins tests
TARGET_GROUP_ID="$(.venv/bin/python -c 'import secrets; print(secrets.randbelow(900000000) + 100000000)')" .venv/bin/python -m unittest \
  tests.test_chat_vision_store tests.test_chat_vision_files \
  tests.test_chat_vision_client tests.test_chat_vision_ingestion \
  tests.test_group_router tests.test_random_chat tests.test_random_chat_context \
  tests.test_chat_archive tests.test_evidence_capture tests.test_evidence_store \
  tests.test_plugin_loading tests.test_public_source -v
git diff --check
```

Expected: compilation and all selected tests pass; diff check prints nothing.

- [ ] **Step 3: Run the full suite with a non-public synthetic group ID**

Run:

```bash
TARGET_GROUP_ID="$(.venv/bin/python -c 'import secrets; print(secrets.randbelow(900000000) + 100000000)')" \
  .venv/bin/python -m unittest discover -s tests -q
```

Expected: the complete suite passes with zero failures and errors.

- [ ] **Step 4: Commit documentation**

```bash
git add .env.example README.md CHANGELOG.md tests/test_public_source.py
git commit -m "docs: explain chat vision memory"
```

- [ ] **Step 5: Deploy with evidence-preserving rollback**

On production, record the current Git commit; back up `chat_archive.db`, `.env`, and the evidence database; record a recursive file count and SHA-256 manifest for `evidence/`; create a Git rollback branch. Fast-forward to the verified commit, set only the new `CHAT_VISION_*` values with `CHAT_VISION_ENABLED=true`, run schema initialization, and restart the bot once.

Verify both services are active, OneBot is connected, the new table exists, and `PRAGMA quick_check` is `ok`. Send one new pure image and one addressed image in an allowed group, then verify new visual rows and descriptions. Finally compare the evidence file count and manifest to the pre-deploy values; any evidence difference stops acceptance and triggers rollback investigation.
