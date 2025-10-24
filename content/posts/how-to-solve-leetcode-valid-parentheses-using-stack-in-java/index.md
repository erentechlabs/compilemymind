---
title: "How to Solve LeetCode's Valid Parentheses Problem Using Stack in Java"
description: "In this post, we walk through a beginner-friendly solution to LeetCode's Valid Parentheses problem using the Stack data structure in Java. We explain the logic step by step, making it easy to follow and apply in interviews or practice."
date: 2025-08-06
tags: ["java", "algorithms", "stack", "leetcode"]
categories: ["technology"]
---

LeetCode problems can sometimes be frustrating, but other times they can be genuinely fun. What I want to talk about today definitely falls into the fun category. 😊

Imagine we only have the characters (), {}, and [] to work with. We want to check whether every opening parenthesis, square bracket, and curly brace is correctly closed in the given string.

For example, inputs like () or ({[]}) are valid, while something like ()[]{}{ would be invalid because of the unclosed brace at the end.

There are several ways to solve this, but today I’ll walk you through a method that uses a data structure called a Stack.

### Quick Refresher: What is a Stack?
A Stack is a data structure that stores items in a ***last-in***, ***first-out (LIFO)*** manner. Think of it like a stack of plates—what goes in last comes out first. Here's a simple visual to imagine it:
![Stack](/images/technology/stack.jpg)

Now let’s apply this to our parentheses validation problem.

### Step-by-Step Solution
We will write a method that takes a string input, such as "()[]{}", and returns true if the parentheses are valid, or false if they’re not.

### 1. Define the Method
   
```Java
public static boolean isValid(String s)
```

The input s will be a string like "()[]{}". We return a boolean indicating whether the parentheses are valid.

### 2. Declare a Stack
   
We’ll use a Stack to keep track of opening brackets:

```Java
Stack<Character> stack = new Stack<>();
```

### 3. Iterate Through the Characters
   
We loop through each character in the string:

```Java
for (char c : s.toCharArray())
```

### 4. Push Opening Brackets onto the Stack
   
If we encounter an opening bracket, we push it onto the stack:

```Java
if (c == '(' || c == '[' || c == '{') {
    stack.push(c);
}
```

### 5. Handle Closing Brackets
   
If we encounter a closing bracket, we first check if the stack is empty. If it is, that means there was no corresponding opening bracket, so we return false.

Then, we look at the top of the stack using peek() (without removing it) to check if it matches the closing bracket. If it doesn’t match, we return false. If it does, we remove the top of the stack using pop():

```Java
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

### 6. Final Validation

If the stack is empty at the end, that means all opening brackets had matching closing brackets. Otherwise, some were left unmatched.

```Java
return stack.isEmpty();
```

### Summary
Here’s the complete method:

```Java
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