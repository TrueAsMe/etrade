# `calculate_sets` — Debug Notes
### Bug: `UnboundLocalError: cannot access local variable 'rr_t2'`
---

## Error

```
Cell In[29], line 93
--> 93     "rr_t2": rr_t2,
UnboundLocalError: cannot access local variable 'rr_t2' where it is not associated with a value
```

Triggered from the QA cell calling `calculate_sets(x, z)`.

---

## Root Cause

The `elif rr_raw <= 3.0` branch was missing two variable assignments that the
return dict at line 93 references: `rr_t2` and `contracts_t2`.

**Why Python raises `UnboundLocalError` here specifically:**
Python scans the entire function body at *compile time* — before a single line
runs. It sees `rr_t2` assigned inside the `elif rr_raw <= 6.0` and `else`
branches, so it marks `rr_t2` as a *local variable* for the whole function.
Then at *runtime*, when execution takes the `elif rr_raw <= 3.0` path (where
there is no assignment), Python tries to read a local that was never written
in that path — hence "cannot access local variable."

Adding `rr_t2 = 0` (and `contracts_t2 = 0`) to every branch that does not
compute a real value is the fix.

The branch also had three duplicate `t2_points = 0` assignments — only one is
needed.

---

## Broken Branch (before fix)

```python
elif rr_raw <= 3.0:
    # Single target — full position exits at opposing zone
    t1           = round(entry + direction * stop_points * 2.5, 1)
    t2           = None
    t1_points    = round(abs(t1 - entry), 1)
    t2_points    = 0
    rr_t1        = round(t1_points / stop_points, 2)
    t2_points    = 0          # duplicate
    contracts_t1 = contracts
    t2_points    = 0          # duplicate
    # rr_t2       ← MISSING → UnboundLocalError at return dict
    # contracts_t2 ← MISSING → UnboundLocalError at return dict
```

---

## Fixed Branch

```python
elif rr_raw <= 3.0:
    # Single target — full position exits at opposing zone
    t1           = round(entry + direction * stop_points * 2.5, 1)
    t2           = None
    t1_points    = round(abs(t1 - entry), 1)
    t2_points    = 0
    rr_t1        = round(t1_points / stop_points, 2)
    rr_t2        = 0                                   # added
    contracts_t1 = contracts
    contracts_t2 = 0                                   # added
```

Changes made:
1. Removed the two duplicate `t2_points = 0` lines.
2. Added `rr_t2 = 0` after `rr_t1`.
3. Added `contracts_t2 = 0` after `contracts_t1`.

The `elif rr_raw <= 6.0` and `else` branches were already correct — not touched.

---

## Full Two-Target Logic Block (corrected)

```python
# ── TWO-TARGET LOGIC ─────────────────────────────────────────
if rr_raw < 2.0:
    # Reject — insufficient R:R
    return None
elif rr_raw <= 3.0:
    # Single target — T1 at 2.5x stop
    t1           = round(entry + direction * stop_points * 2.5, 1)
    t2           = None
    t1_points    = round(abs(t1 - entry), 1)
    t2_points    = 0
    rr_t1        = round(t1_points / stop_points, 2)
    rr_t2        = 0
    contracts_t1 = contracts
    contracts_t2 = 0
elif rr_raw <= 6.0:
    # Two targets — T1 at 2.5x stop, T2 at t2_cap * stop
    t1           = round(entry + direction * stop_points * 2.5, 1)
    t2           = round(entry + direction * stop_points * t2_cap, 1)
    t1_points    = round(abs(t1 - entry), 1)
    t2_points    = round(abs(t2 - entry), 1)
    rr_t1        = round(t1_points / stop_points, 2)
    rr_t2        = round(t2_points / stop_points, 2)
    contracts_t1 = contracts
    contracts_t2 = contracts - contracts_t1 if contracts >= 2 else 0
else:
    # Two targets — T1 at 3.0x stop, T2 at t2_cap * stop
    t1           = round(entry + direction * stop_points * 3.0, 1)
    t2           = round(entry + direction * stop_points * t2_cap, 1)
    t1_points    = round(abs(t1 - entry), 1)
    t2_points    = round(abs(t2 - entry), 1)
    rr_t1        = round(t1_points / stop_points, 2)
    rr_t2        = round(t2_points / stop_points, 2)
    contracts_t1 = max(1, contracts // 2)
    contracts_t2 = contracts - contracts_t1 if contracts >= 2 else 0
```

---

## Key Concept: Python Variable Scoping in if/elif/else

In Python, there is no block scope — `if`, `elif`, and `else` do not create
their own namespaces. Any variable assigned *anywhere* in a function is
treated as local to the entire function from the moment Python compiles it.

This means:

```python
def example():
    if condition:
        x = 1        # Python marks x as local to the whole function
    return x         # UnboundLocalError if condition was False
```

The safe pattern when a variable may not be set in every branch is to either:
- Assign it in every branch (as done here: `rr_t2 = 0`, `contracts_t2 = 0`)
- Or initialise a default before the if/elif/else block:
  ```python
  rr_t2 = 0
  contracts_t2 = 0
  if ...:
      ...
  ```

Both approaches are valid. Assigning inside each branch makes the intent
explicit per case; assigning a default before the block reduces repetition.