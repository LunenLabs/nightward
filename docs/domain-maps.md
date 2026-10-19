# Domain maps

[nightward](../README.md) reference. A domain map writes down which rules each
captured behavior guards, so a reviewer sees what a value should be next to what
it became.

The `nightward:onboard` skill in the Claude Code plugin writes the maps before the
first capture. It reads the code without changing it, finds a second source for
each rule's intent, and lists where the code and the intent disagree. You can also
write maps by hand.

## Where and what

One Markdown file per group in `docs/nightward-domain/`, relative to
`pyproject.toml`. Set `[tool.nightward] domain = "other/folder"` to move them.
nightward reads only the TOML front matter between `+++` lines; the body is free
prose for people:

```markdown
+++
group = "billing"
derived_from = "bd28deb"              # the commit that was analysed

[rules.R1]
rule = "Orders of 50.00 or more ship free"
code = "shop.py:42"
intent = "docs/pricing.md, 'Shipping'"

[rules.R2]
rule = "Tax rounds half up to the cent"
code = "shop.py:17"
# no `intent`: unconfirmed, inferred from the code alone

[behaviors]
"checkout_total" = ["R1", "R2"]
+++

# Billing

The state and input matrix, suspected defects, open questions...
```

## What nightward does with them

At the `detailed` and `exhaustive` [review depths](review-depth.md), `nightward
review` prints each rule above the behavior's diff, with its code location, its
intent source, and the map file. A rule without `intent` reads *unconfirmed:
inferred from the code alone*. A map written only from the code describes the
code's bugs as rules, so treat those as questions, not facts.

A map is a display aid. One that can't be read (broken front matter, an unknown
rule id) prints a warning, and the review goes on. A map never approves, rejects
or changes a verdict.
