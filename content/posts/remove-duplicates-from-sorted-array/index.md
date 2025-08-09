---
title: "Remove Duplicates from Sorted Array — The Two-Pointer Solution"
description: "Learn how to remove duplicates from a sorted array in Java, explained step-by-step with a beginner-friendly approach using the two-pointer technique."
date: 2025-08-09
tags: ["Java", "Algorithms", "Data Structures", "Problem Solving", "Coding Interview", "Two Pointers", "LeetCode", "Programming"]
categories: ["technology"]
---

Removing duplicates from a sorted array is a common coding interview question.  
It’s simple at first glance, but it’s also a great way to learn **in-place array manipulation** and the **two-pointer pattern**.

In this article, we’ll break it down so even if you’re new to Java, you’ll understand not just *how* to do it, but *why* it works.

---

## Why This Problem is Important

- **Teaches two-pointer logic:** A fundamental pattern in algorithm design.
- **In-place modification:** Useful when you want to save memory.
- **Common interview question:** Appears often in technical screenings.

---

## Problem Overview

We are given:
- An integer array `nums`, sorted in **non-decreasing order**.
- We need to:
  1. Remove duplicates so that each unique element appears only once.
  2. Keep the **relative order** of elements the same.
  3. Modify the array **in-place** without creating another array.
  4. Return `k`, the number of unique elements.

The values beyond index `k-1` don’t matter.

---

## Example

```Java
int[] nums = {0, 0, 1, 1, 1, 2, 2, 3, 3, 4};
```

**Output**

`k = 5`

The array `nums` is modified to:  
`[0, 1, 2, 3, 4, _, _, _, _, _]` 

The underscores mean that the values at those indices are irrelevant after processing.

---

## Thought Process

Because the array is already sorted:
- All duplicates will be **next to each other**.
- This makes it easy to detect duplicates by comparing the current element with the previous one.

We can:
1. Use a pointer `k` to track where the **next unique** element should go.
2. Iterate through the array starting from index `1`.
3. If the current element is different from the previous one, put it at index `k` and increment `k`.

---

## Step-by-Step Flow

Imagine `nums = [0,0,1,1,1,2,2,3,3,4]`:

1. Start with `k = 1` (because the first element is always unique).
2. Move through the array with index `i`:
   - If `nums[i] != nums[i - 1]`, it’s a new number → put it at `nums[k]`, then `k++`.
3. At the end, `k` tells us how many unique numbers there are.

---

## Complexity

- **Time Complexity:** O(n) → We scan the array once.
- **Space Complexity:** O(1) → No extra array, we modify in-place.

---

## Final Code

```Java
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