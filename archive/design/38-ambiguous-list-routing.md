# 38 — When Tier 0 says "I don't know", the dialog answers for the user

> **Status: proposal, nothing built.** §2–§3 are measured on the committed
> fixtures on 2026-08-23. The two defects this document *doesn't* cover
> (CSV rejected at the door, title row breaking the parser) were fixed the same
> day; this one is left open because it needs a product decision, not a patch.

## 1. Trigger

Dragging `金桥地体上盖项目-采购清单.xlsx` into the workspace produced a card
badged **报价清单** carrying a raw parser exception. The misrouting, not the
exception, is what this document is about.

> **Update 2026-08-23 — §2's premise was wrong, and this file is no longer
> ambiguous.** Re-measuring the columns cell by cell showed the 63% fill rate was
> an artifact: `单价（元）不含税` is entirely blank and `合计（元）不含税` /
> `税额（元）` / `价税合计（元）` are non-empty but hold **literally `0` in every
> row**. There is no price in the file. Counting a written `0` as "filled" is
> right per cell and wrong per column — 89 items at zero is a placeholder, not a
> quote. Two judgements were repaired (all-zero price column is not a price
> column; "price headers, empty cells" now maps to 采购清单 via `FILL_RATE_BLANK`),
> and this file now classifies as **tender_list / definitive**.
>
> **§3 and §4 still stand and are still unfixed.** The dialog is what turned a
> misclassification into a misrouting, and the next genuinely ambiguous file will
> hit exactly the same path — dismissing the dialog still counts as answering
> "投标文件". The concrete harm §3 predicted was observed in the running app: the
> procurement list became a supplier column in the comparison matrix (0/89,
> total ¥0). §5's definitional question is likewise unchanged: a procurement list
> that ships with real 控制价 is still unclassifiable by design/28 §2's
> definition, and that is the case the dialog exists for.

## 2. The file is genuinely ambiguous, and Tier 0 says so correctly

```
verdict      = uncertain
confidence   = ambiguous
price_columns = ['价税合计（元）', '单价（元）\n不含税', '合计（元）\n不含税']
fill_rate    = 63%
```

design/28 §2's rule is that a procurement list is defined by being **blank** —
prices are what the bidder fills in. This file has three price columns, 63%
filled. Its own title row reads 「…研发及商业项目**阀门投标清单**」. By the
stated rule it is not a procurement list, and Tier 0 refusing to decide is the
correct behaviour, not a bug.

Compare 徐汇: no price columns at all → `tender_list (definitive)`. That path
works end to end.

So the question this document has to answer is not "how do we classify better".
It is **what happens after an honest "I don't know"**.

## 3. What happens today

`classifyAndRouteFile` sends every `uncertain` Excel to `askTenderOrBid`:

```
Modal.confirm({
  okText: '招标文件',
  cancelText: '投标文件',
  onOk:     () => routeToTender(file, card),
  onCancel: () => routeToBid(file, card, inferBidDocKind(file.name)),
})
```

**The cancel path is a real answer.** `Modal.confirm` fires `onCancel` on the
cancel button, on Escape, and on a mask click. A user who dismisses the dialog
— to go look at the file, to deal with another card, by reflex — has silently
told the system "this is a bid document", and the file is routed, uploaded and
parsed as one.

There is no third outcome. The dialog cannot be closed without choosing.

That is how the 金桥 procurement list ended up on the quote path, where it then
hit the (now-fixed) header bug and produced a red card. Fixing the parser makes
the failure quieter, not more correct: the file would now parse *successfully*
as somebody's quote, which is worse — a procurement list silently entering the
system as a bid, with a supplier column that isn't there.

## 4. Two problems, stated separately

### 4.1 A dismissal is not an answer

Escape means "not now". Binding it to one of two substantive choices means the
system records a decision the user never made. Whatever the dialog's options
end up being, dismissal has to leave the file **unrouted and visible** — the
card stays, badged 「待你确认类型」, with the choice reachable again.

design/29 §10 already established that a file must never lose its card. This is
the same principle one level down: a file must never lose its *undecided* state
either.

### 4.2 「招标文件 / 投标文件」 is the wrong question

The dialog asks who *provided* the file. But the routing that follows is by
what the file *is*: `routeToTender` starts PDF tender recognition,
`routeToBid` starts quote extraction. For an ambiguous Excel the useful
question is the second one — 「这是要各家去填的清单，还是某一家已经填好的报价？」
— and it has a third honest answer the current dialog cannot express:
**「都不是 / 我不确定」**.

The 金桥 file is exactly that case. It is a procurement list that arrives with
reference prices already in it. Neither existing branch is right for it.

## 5. The definitional gap underneath

design/28 §2 defines 采购清单 as "blank, the bidder fills it in". Real
procurement lists sometimes ship with 控制价 / 参考价 / 暂定价 already filled.
Under the current definition those files are unclassifiable **by construction**,
and every one of them will land in this dialog.

This is the decision that has to be made before the dialog can be fixed, because
it determines whether the third option is a real category or just an escape
hatch. Options:

- **(a) Keep the definition, add a category.** A procurement list may carry
  reference prices; the price columns get a role (`reference_price`) distinct
  from a supplier's quote. Most faithful to reality, most work: the anchor
  schema, the preview, and the matrix all have to know that a row can have a
  tender-side reference price that is not a bid.
- **(b) Keep the definition, treat priced lists as out of scope.** The dialog's
  third option is 「这份我先不处理」and the file stays parked. Cheap, honest,
  and leaves a real workflow unserved.
- **(c) Widen the definition by ignoring price columns on the tender side.**
  Parse it as a procurement list and drop the prices. Cheapest to build and the
  worst of the three — silently discarding data the user can see in the file is
  the failure mode CLAUDE.md's "no silent fill" rule exists to prevent, run in
  reverse.

Recommendation: **(b) now, (a) when a real 控制价 workflow is asked for.** (b)
costs almost nothing and stops the misrouting immediately; (a) is a genuine
feature and should be scoped as one, not smuggled in through a dialog.

## 6. What was fixed already, and is not up for discussion here

Both were found in the same session and were unambiguous defects:

- **CSV rejected at the door.** `classify_tier0` returned `None` for `.csv`, so
  the route answered `kind="unsupported"` and the card read 「不支持的文件类型」
  — while `extract_quote_tabular` parsed all four 徐汇 quote CSVs at 136 items
  each. The capability was there; the door was shut. `.csv` now goes through
  `classify_excel` (the judgement is about columns, not container).
- **Title row above the header.** `_load_dataframe` hard-coded `header=0`, so
  any workbook with a title line failed with
  「未识别到物料名称列。实际列名：['<整行标题>', 'Unnamed: 1', …]」. Both
  procurement lists have one. It now scans for the header row using
  `_detect_tabular_columns` as the judgement — the same thing
  `parse_tender_xlsx` and the test suite's `read_reference` already did.

Recorded here because §3's failure was only *visible* thanks to the second one.
With both fixed, the misrouting in §4 becomes silent, which raises the priority
of this document rather than lowering it.

## 7. Decisions needed

1. **§5** — (a), (b), or (c). Nothing else here can be built until this is
   settled.
2. **§4.1** — confirm that dismissal must leave the file unrouted. This means a
   card can sit in 「待你确认类型」 indefinitely; the upload count and the
   「进入正式比价」 gate both need to account for it.
3. **§4.2** — reword the dialog from 「谁提供的」 to 「这是什么」? The provider
   question still matters for PDFs (招标文件 vs 投标文件 is a real distinction
   there), so this may mean two different dialogs rather than one.
