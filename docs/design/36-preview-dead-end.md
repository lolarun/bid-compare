# 36 — The preview screen is a dead end

> **Status: proposal, nothing built.** §2 is what the running app does today,
> verified by reading the code paths and by manual testing on a real project
> (2026-08-23). §5 has the decisions that need an explicit answer.

## 1. Trigger

User, 2026-08-23, after previewing a real 3-supplier project:

> 我在界面中点「纳入」并不好使，而且这些提示，我感觉毫无意义，另外我现在
> 无法点击比价

Three complaints, one disease: **the preview screen shows the user what is
wrong, and offers no action that actually moves them forward.** Everything on
that screen is either inert, unreadable, or points somewhere else.

## 2. What the app does today

### 2.1 「纳入」/「排除」 are inert — twice over

`BidMatrix.vue` renders inline confirm buttons on every `pending` cell and
emits `confirmItem`:

```
<!-- Inline confirm buttons (only if parent listens to confirmItem) -->
@click.stop="emit('confirmItem', cell.item_id!, 'align')"
```

**`WorkspaceView.vue` never listens for that event.** A grep for `confirmItem`
across the workspace returns only the emit side. Clicking does nothing — no
call, no error, no feedback. The component's own comment ("only if parent
listens") shows the author knew a listener was required; the workspace is the
one caller that never wired it.

**Wiring it up would still not work**, and this is the more important half.
Preview runs the whole official chain inside `preview_sandbox`, which
**rolls back on exit** by design (design/31 §4.1 — it exists precisely so the
sandbox writes cannot survive). The `item_id` printed on a preview cell refers
to a `BidAlignmentItem` that **no longer exists by the time the user sees the
screen**. Any confirm call against it can only 404.

So the buttons are not "unwired" — they are **structurally impossible** in the
preview lane. Wiring them is the wrong fix.

**There is already a correct implementation elsewhere.**
`AnchorReviewMatrix.vue` (reached from the header's 「对齐核查」) has a real
`confirmItem` that calls the API and reports 「已纳入矩阵」/「已排除」, plus
candidate selection and missing-acknowledgement. The buttons in `BidMatrix` are
a stripped copy of that, in a lane where the data they point at is gone.

### 2.2 The doubt notes are internal metric names

Verbatim from the screen:

> 「上海泰科龙阀门有限公司」已进入预览，但有疑点：6 行未通过结构完整性检查
> （**列错位 6 行 / 重复 0 行 / 算术不闭合 0 行，重复金额占比 0.0%，算术错误率
> 1.1%**）。系统不会代为删除或重排，请核对原文后逐行确认。

Five internal check names, three of them reporting zero. "列错位" is a term
from `draft_integrity.py`, not from procurement. And the closing instruction —
"请核对原文后逐行确认" — names no place to do it.

**This is the same complaint the user filed on 2026-08-12** (recorded in
`project_manual_test_feedback_0812`, item ⑤): «质量门提示全是内部指标名，客户
读不懂也没有操作入口». It was recorded and deliberately not scheduled. It is
still here.

One line is worse than jargon — it is vacuous:

> 预览口径 · 含 **89 行未确认报价**，不作为定标依据

In preview nothing is ever confirmed, so "89 行未确认" is 89 out of 89 — always
the total, in every preview, for every project. It carries no information.

### 2.3 There is no visible way to reach 比价

`进入正式比价` is gated on `canCompare`, which requires at least one supplier
to have completed 「确认入库」:

```
const confirmedSubmissionIds = computed(() =>
  batchFiles.value.filter((f) => f.confirmed && ...))
const canCompare = computed(() => confirmedSubmissionIds.value.length > 0)
```

The 「确认入库」 button lives in **the per-supplier tab of the detail view**
(`WorkspaceView.vue`, inside the supplier `a-tab` next to 「重新核对」/「移除」).
It is not on the preview screen.

So the preview screen states 「已确认入库 0 / 3 家」 — a requirement — while
showing no control that satisfies it, and the one control that *looks* like it
might (「纳入」) is inert. That is the dead end.

## 3. Why these are one problem, not three

The preview lane was designed to answer «货比三家谁便宜» before committing
anything (design/31). It does that well — the matrix is correct and the
ordering queue genuinely ranks doubts by money at stake.

What it lacks is **a next step**. Every affordance on the screen either does
nothing (§2.1), describes an internal check (§2.2), or names a prerequisite
without offering it (§2.3). A user who has just been told "10 items need
confirming, worth ¥14,445" has no button that begins confirming them.

## 4. Proposed approach

### 4.1 Remove the inert buttons from the preview lane

Hide 「纳入」/「排除」 when the matrix is rendered with `basis="preview"`.
Keep them in `AnchorReviewMatrix`, where the data is real and the calls work.
Leaving a button that cannot work is worse than having no button: it spends the
user's trust on a click that silently does nothing.

`BidMatrix` already takes a `showHistory` prop for exactly this kind of
lane-dependent difference; the same pattern applies.

### 4.2 Give the preview screen the action it is missing

Put the real next step on the preview screen: a primary 「校对入库」 control
that names who is still outstanding (「还差 2 家：凯硕、泰科龙」) and takes the
user to that supplier's detail tab. The action already exists and works —
`confirmBatchEntry` — it is only unreachable from here.

The ordering queue should be clickable for the same reason: each row already
knows its supplier and anchor, so 「看原文依据」's neighbour should be a jump to
that row in the detail view.

### 4.3 Rewrite the doubt notes as consequences, not check names

Each note answers three questions in the user's vocabulary: **what is wrong,
what it costs, where to fix it.**

| Today | Proposed |
|---|---|
| 6 行未通过结构完整性检查（列错位 6 行 / 重复 0 行 / 算术不闭合 0 行，重复金额占比 0.0%，算术错误率 1.1%） | 泰科龙有 6 行的数据没对齐表头，金额不可信 → 去核对这 6 行 |
| 1 行原文无合价。系统不会代为计算 | 绵存有 1 行原文没有合价，系统不会替你算 → 去补这一行 |
| 预览口径 · 含 89 行未确认报价 | *(删除)* |

Rules this encodes, worth stating because they are what went wrong:

- **Never print a check whose count is zero.** Three of the five numbers in
  today's note are noise.
- **Never print an internal identifier** (`列错位`, `算术错误率`,
  `structural_integrity_requires_review`) in user-facing text.
- **Every doubt names its destination.** "请核对原文后逐行确认" without a link
  is a dead end by construction.

### 4.4 Keep the honest parts

The banner must keep saying preview is not a basis for award (`basis="preview"`
is a contract-level guarantee, not a UI nicety), and the ordering queue's
「影响无从估算」 must stay — it is the honest form of "we don't know", and
replacing it with ¥0 would read as "negligible".

## 5. Decisions needed

1. **§4.1** — remove the preview-lane confirm buttons, or invest in making
   preview-lane confirmation real? Making it real means the sandbox would have
   to persist alignment decisions, which contradicts design/31's "preview never
   writes". Recommendation: remove.
2. **§4.2** — should 「校对入库」 on the preview screen confirm **all clean
   suppliers at once**, or always route to the per-supplier detail tab?
   Bulk confirm is faster but hides which file had which doubt.
3. **§4.3** — the rewritten notes need a per-check phrasing table. Should that
   live in the frontend (a map from check id → sentence) or come from the
   backend as a `user_message` field alongside the existing machine fields?
   Recommendation: backend, so exports and future channels say the same thing;
   the machine fields stay for diagnostics.

## 6. Out of scope

- The recognition gaps behind the doubts themselves (design/33 名称列读错,
  design/34 识别空洞). This document is about the screen, not the numbers.
- `AnchorReviewMatrix` — it already does per-row confirmation correctly and is
  not being changed.
