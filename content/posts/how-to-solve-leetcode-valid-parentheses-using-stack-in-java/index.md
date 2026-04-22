---
title: "Solving LeetCode's Valid Parentheses Problem with a Stack in Java"
description: "A beginner-friendly walkthrough of the Valid Parentheses problem — using the stack data structure to implement a clean, O(n) solution in Java."
date: 2025-08-06
tags: ["Programming", "Java", "Algorithms", "ProblemSolving"]
categories: ["technology"]
---

Some LeetCode problems feel like busy work. The Valid Parentheses problem is not one of them. It's a clean, well-defined problem that teaches a genuinely useful pattern — the **stack** — and produces code that's actually elegant when you get it right.

The problem: given a string containing only `(`, `)`, `{`, `}`, `[`, and `]`, determine if the input is valid. A string is valid if every opening bracket is closed by the correct bracket type, in the correct order.

```
"()" → valid
"()[]{}" → valid
"({[]})" → valid
"([)]" → invalid (wrong order)
"{[]" → invalid (unclosed bracket)
```

---

## The Right Data Structure

The key insight: **you only ever need to remember the most recently opened bracket**. When you encounter a closing bracket, you need to check if it matches the *last* thing you opened.

That's a last-in, first-out (LIFO) pattern — exactly what a **stack** provides.

```
Processing: ( { [ ] } )

Push (   → stack: [(]
Push {   → stack: [(, {]
Push [   → stack: [(, {, []
See ]    → peek: [ → match! pop → stack: [(, {]
See }    → peek: { → match! pop → stack: [(]
See )    → peek: ( → match! pop → stack: []
Done     → stack empty → valid ✓
```

If at any point the closing bracket doesn't match the top of the stack, the string is invalid. If the string ends with items still in the stack, some brackets were never closed — also invalid.

---

## Step-by-Step Implementation

### Define the Method Signature

```java
public static boolean isValid(String s)
```

Input is a string. Output is a boolean. Simple.

### Create the Stack

```java
Stack<Character> stack = new Stack<>();
```

We'll push opening brackets onto this stack as we encounter them.

### Iterate Through the String

```java
for (char c : s.toCharArray()) {
```

Process one character at a time.

### Handle Opening Brackets

When we see an opener, push it:

```java
if (c == '(' || c == '[' || c == '{') {
    stack.push(c);
}
```

### Handle Closing Brackets

When we see a closer, two things can go wrong:

1. The stack is empty — there's nothing to match against
2. The top of the stack doesn't match the current closer

```java
else {
    if (stack.isEmpty()) return false;

    char top = stack.peek();

    if ((c == ')' && top != '(') ||
        (c == ']' && top != '[') ||
        (c == '}' && top != '{')) {
        return false;
    }

    stack.pop();
}
```

Note: we use `peek()` to check without removing, then `pop()` only after confirming the match.

### Final Check

After processing every character, the stack should be empty. Any leftover items mean unclosed brackets:

```java
return stack.isEmpty();
```

---

## The Complete Solution

```java
public static boolean isValid(String s) {
    Stack<Character> stack = new Stack<>();

    for (char c : s.toCharArray()) {
        if (c == '(' || c == '[' || c == '{') {
            stack.push(c);
        } else {
            if (stack.isEmpty()) return false;

            char top = stack.peek();

            if ((c == ')' && top != '(') ||
                (c == ']' && top != '[') ||
                (c == '}' && top != '{')) {
                return false;
            }

            stack.pop();
        }
    }

    return stack.isEmpty();
}
```

---

## Complexity Analysis

| Metric | Value |
|--------|-------|
| **Time** | O(n) — each character is processed once |
| **Space** | O(n) — worst case, all openers pushed to stack |

The space complexity is O(n) in the worst case (a string of `n` opening brackets with no closers). In practice, for valid strings, the stack depth stays bounded.

---

## Why This Pattern Matters

The stack-based bracket matching pattern appears in real software:

- **Compilers** — validating syntax trees and expression nesting
- **Text editors** — bracket highlighting and auto-close features  
- **XML/HTML parsers** — validating tag nesting
- **JSON parsers** — validating object and array nesting
- **Security tools** — parsing firewall rules, network protocols, file formats

Understanding the stack pattern is understanding how a significant portion of parsing works. The Valid Parentheses problem is a good entry point precisely because the problem is small enough to fully grasp in one sitting, but the underlying pattern scales to complex real-world applications.