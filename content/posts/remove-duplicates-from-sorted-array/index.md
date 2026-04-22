---
title: "Remove Duplicates from a Sorted Array — The Two-Pointer Technique"
description: "A clean walkthrough of the Remove Duplicates from Sorted Array problem in Java, with focus on the two-pointer pattern and in-place array manipulation."
date: 2025-08-09
tags: ["Programming", "Java", "Algorithms", "ProblemSolving"]
categories: ["technology"]
---

Removing duplicates from a sorted array is one of those problems that sounds trivial until you add the constraint: do it **in-place**, without allocating another array. That constraint is what makes it interesting and what makes the **two-pointer pattern** the right tool.

The problem: given a sorted integer array `nums`, remove duplicates in-place so each unique element appears exactly once. Return `k` — the count of unique elements. The values at `nums[0]` through `nums[k-1]` must be the unique elements in order; anything beyond index `k-1` doesn't matter.

```java
Input:  [0, 0, 1, 1, 1, 2, 2, 3, 3, 4]
Output: k = 5
        nums = [0, 1, 2, 3, 4, _, _, _, _, _]
```

---

## The Key Observation

Because the array is already sorted, all duplicates are adjacent. `[0, 0, 1, 1, 2]` — the duplicates cluster together, never scattered. This is the property we exploit.

We don't need a hash set or any additional storage. We just need to *walk* the array and *write* unique values to the front as we find them.

---

## The Two-Pointer Pattern

We maintain two "pointers" (really just indices):

- **`i`** — scans forward through the entire array (the "reader")
- **`k`** — points to where the next unique element should go (the "writer")

The logic:

1. Start with `k = 1` — the first element is always unique
2. For each position `i` starting at 1:
   - If `nums[i] != nums[i-1]` → we've found a new unique value → write it at `nums[k]`, increment `k`
   - If `nums[i] == nums[i-1]` → duplicate → skip it
3. Return `k`

---

## Walking Through an Example

```
nums = [0, 0, 1, 1, 1, 2, 2, 3, 3, 4]
k = 1

i=1: nums[1]=0, nums[0]=0  → same, skip
i=2: nums[2]=1, nums[1]=0  → different → nums[1]=1, k=2
i=3: nums[3]=1, nums[2]=1  → same, skip
i=4: nums[4]=1, nums[3]=1  → same, skip
i=5: nums[5]=2, nums[4]=1  → different → nums[2]=2, k=3
i=6: nums[6]=2, nums[5]=2  → same, skip
i=7: nums[7]=3, nums[6]=2  → different → nums[3]=3, k=4
i=8: nums[8]=3, nums[7]=3  → same, skip
i=9: nums[9]=4, nums[8]=3  → different → nums[4]=4, k=5

Result: nums = [0, 1, 2, 3, 4, ...], k = 5
```

The array modifies itself in-place. No extra memory. One pass.

---

## The Implementation

```java
public static int removeDuplicates(int[] nums) {
    int k = 1;

    for (int i = 1; i < nums.length; i++) {
        if (nums[i] != nums[i - 1]) {
            nums[k] = nums[i];
            k++;
        }
    }

    return k;
}
```

That's it. Six lines. Everything else is the explanation.

---

## Complexity

| Metric | Value | Reason |
|--------|-------|--------|
| **Time** | O(n) | Single pass through the array |
| **Space** | O(1) | No additional data structures |

This is as efficient as it gets — linear time, constant space.

---

## Why In-Place Manipulation Matters

In-place array algorithms are a fundamental class of problems in systems programming, performance-critical code, and embedded systems where memory is constrained. The two-pointer pattern — maintaining a slow pointer for writing and a fast pointer for reading — appears in:

- String manipulation without extra allocation
- Partitioning arrays (used in quicksort)
- Merging sorted arrays in-place
- Many competitive programming and interview problems

Understanding this pattern is about more than passing interviews. It's about having a mental model for solving a whole category of problems efficiently.