# Handoff: Keanu menu-agent fixes (loop cap + tool replay)

Diagnosed 2026-07-19 from a session in ~/projects/personal. Make the changes in
this doc from a Claude session running in this project. The MenuBuilder side has
ALREADY been fixed (see bottom) — no bridge contract changes, nothing to wait
on, and no restart needed there (the bridge spawns a fresh subprocess per call).

## Task 1 — Agent goes silent when a turn needs 5+ tool calls

### Symptom
David texted "Yes just get a random recipe for each" at 11:04 AM 2026-07-19.
Keanu executed five `swap_meal` calls (logged 11:04:04–11:04:14), saved them
all, then never texted a reply. Looked like a hang; process was healthy.

### Root cause
`agents/menu_workflow.py`, agent loop (~line 904):
- `for _ in range(5)` — hard cap of 5 API rounds per turn.
- `tool_choice` sets `disable_parallel_tool_use: True`, so max one tool call
  per round.
- A turn needing 5 tool calls consumes every round on tool_use responses; the
  loop exits without reaching the branch that sets `final_reply`, so `""` is
  returned and no SMS is sent.

### Fix
1. **Finisher call (the real fix).** After the loop, if `final_reply` is still
   empty, make one more `client.messages.create` with the accumulated
   `messages`, keeping the `tools` param but passing
   `tool_choice={"type": "none"}` so the model must produce text summarizing
   what it did. Same try/except; fall back to a static
   `"Done — text 'menu' to see the updated week."` on error.
2. **Raise the cap** to `range(8)` so the finisher is rarely needed.

### Verify
1. Temporarily set the cap to 2, send a message needing 3+ tool calls
   ("swap Mon, Tue, and Wed"), confirm a reply still arrives and swaps saved.
2. Restore the cap; restart Keanu with `launchctl unload` then `launchctl load`
   of com.keanu.sms-assistant.plist (NOT kickstart).
3. Live test a multi-swap request.

## Task 2 — Tool calls replay on every turn (caused real data corruption)

Only plain text turns are persisted to `session["conversation"]`;
tool_use/tool_result blocks are local to each turn. On the next message the
model has no memory of having called tools, so it re-calls them. On 2026-07-19
this re-ran feedback logging + auto-finalization ~5 times: times_cooked was
multi-bumped for three recipes, a meal the family never ate got logged as
cooked 5 times, and inventory protein deductions ran repeatedly.

### Fix
After each tool round, append a compact plain-text note of what was executed to
the persisted conversation (e.g. `"[logged feedback for Creamy Mushroom
Orzotto]"`, `"[generated meal plan]"`), so the model knows the action already
happened. Keep it terse — it goes through the 40-turn cap.

Note: MenuBuilder's `log_meal_feedback` now also drops exact-duplicate feedback
deliveries and replayed finalization is a no-op for "already logged" meals, so
the server side is defended — but the replaying itself is Keanu's bug and
still burns API calls and tool rounds. Fix it here.

## Task 3 — Kill the duplicate server.py process

A `server.py` owned by `davidallison` has been running since Thu ~5 PM
alongside the launchd-managed `allisonbot` one — leftover manual run. Kill it;
only the launchd instance should run. (The plist is also loaded in
davidallison's launchd session per the 7/12 review — unload it there too.)

## Context: MenuBuilder-side changes already done (2026-07-19)

In `~/projects/personal/MenuBuilder/mcp/menu_server.py` (no action needed here,
listed so you know the tool behavior changed):
- `recommend_hold` recipes excluded from candidates and cuisine-injection.
- Selection randomized: shuffle-before-sort, small score jitter, and each day
  picks among the top 3 eligible candidates — no more deterministic
  file-order weeks.
- Cuisine family cap defaults to 2/week (explicit "three Mexican" overrides
  still work, word numbers now parsed).
- `log_meal_feedback` routes by best name-word overlap (not first match) and
  skips duplicate deliveries.
- Data repaired: times_cooked/last_cooked_date corrected for Grilled Chicken
  and Cherry Tomatoes (1, 2026-07-16), Cherry Tomato Salad (0, never),
  Creamy Mushroom Orzotto (2, 2026-07-18); all last-week meals in
  menu_activity.json marked "already logged". Backups:
  recipe_metadata.json.bak-20260719, menu_activity.json.bak-20260719.
