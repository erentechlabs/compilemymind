---
title: "Understanding HTTP Status Codes"
description: "A concise guide to HTTP status codes, their meanings, and practical examples."
date: 2025-11-23
tags: ["Web", "API"]
categories: ["technology"]
---

# Understanding HTTP Status Codes: What They Mean and How to Use Them

When a client (like a web browser) talks to a server, the server responds with an **HTTP status code**. These three‑digit numbers tell the client whether the request succeeded, failed, or needs additional action. Below is a quick guide to the most common codes, grouped by their first digit, with short explanations and practical examples.

---

### 1️⃣ 1xx – Informational  
*The request was received and processing is continuing.*

| Code | Meaning | Example Use |
|------|---------|-------------|
| **100** | Continue – client should keep sending the request body | A large file upload where the server wants to confirm the client should continue sending data. |
| **101** | Switching Protocols – server agrees to switch to a different protocol (e.g., WebSocket) | Upgrading an HTTP connection to a WebSocket for real‑time chat. |

---

### 2️⃣ 2xx – Success  
*The request was successfully received, understood, and accepted.*

| Code | Meaning | Example |
|------|---------|---------|
| **200** | OK – generic success | `GET /index.html` returns the homepage HTML. |
| **201** | Created – a new resource was created | `POST /users` creates a new user and returns its URL. |
| **204** | No Content – request succeeded but no body is returned | `DELETE /posts/123` removes a post; the client gets no content back. |

---

### 3️⃣ 3xx – Redirection  
*Further action needed to complete the request.*

| Code | Meaning | Example |
|------|---------|---------|
| **301** | Moved Permanently – resource has a new permanent URL | Redirect `/old‑blog` to `/new‑blog`. |
| **302** | Found – temporary redirect | Send a user to a login page, then back after authentication. |
| **304** | Not Modified – cached version is still valid | Browser uses cached CSS file, saving bandwidth. |

---

### 4️⃣ 4xx – Client Errors  
*The request contains bad syntax or cannot be fulfilled.*

| Code | Meaning | Example |
|------|---------|---------|
| **400** | Bad Request – malformed syntax | JSON payload missing a required field. |
| **401** | Unauthorized – authentication required | Accessing `/account` without a valid token. |
| **403** | Forbidden – server refuses to fulfill | User tries to delete an admin‑only resource. |
| **404** | Not Found – resource does not exist | Requesting `/nonexistent-page.html`. |
| **429** | Too Many Requests – rate limiting | API client exceeds 100 requests per minute. |

---

### 5️⃣ 5xx – Server Errors  
*The server failed to fulfill a valid request.*

| Code | Meaning | Example |
|------|---------|---------|
| **500** | Internal Server Error – generic server failure | Uncaught exception in backend code. |
| **502** | Bad Gateway – invalid response from upstream server | Reverse proxy receives malformed data from a microservice. |
| **503** | Service Unavailable – server overloaded or down for maintenance | Maintenance window where the API is temporarily offline. |
| **504** | Gateway Timeout – upstream server didn’t respond in time | Timeout while waiting for a database query. |

---

## How to Use These Codes in Your Application

1. **Choose the right code** – Match the outcome of the request to the most specific status code (e.g., prefer `201 Created` over `200 OK` when a new resource is made). 
2. **Include a helpful response body** – For error codes (`4xx`, `5xx`), return a JSON payload with an error message and possibly a machine‑readable error code. 
3. **Leverage redirects wisely** – Use `301` for permanent URL changes (SEO‑friendly) and `302` for temporary flows like login redirects. 
4. **Implement proper caching** – Return `304 Not Modified` when the client’s cached version is still fresh; this reduces bandwidth and speeds up page loads. 
5. **Respect rate limits** – Return `429 Too Many Requests` with a `Retry-After` header so clients know when to try again.

---

### Quick Example: A Minimal Express.js Endpoint

```javascript
// Express.js (Node.js) – returns appropriate HTTP status codes
app.post('/users', async (req, res) => {
  const { name, email } = req.body;
  if (!name || !email) {
    // 400 Bad Request – missing required fields
    return res.status(400).json({ error: 'Name and email are required.' });
  }

  try {
    const user = await createUser(name, email);
    // 201 Created – new user created
    return res.status(201).json({ id: user.id, url: `/users/${user.id}` });
  } catch (err) {
    // 500 Internal Server Error – unexpected failure
    return res.status(500).json({ error: 'Server error, try again later.' });
  }
});
```

In this snippet:
- Missing data → **400**.
- Successful creation → **201**.
- Unexpected failure → **500**.

---

### TL;DR
- **1xx** – informational, rarely used by browsers. 
- **2xx** – success (200 OK, 201 Created, 204 No Content). 
- **3xx** – redirection (301 permanent, 302 temporary, 304 cache). 
- **4xx** – client‑side errors (400 Bad Request, 401 Unauthorized, 404 Not Found). 
- **5xx** – server‑side failures (500 Internal Server Error, 502 Bad Gateway, 503 Service Unavailable).

Understanding and correctly applying these codes makes your API more predictable, debuggable, and user‑friendly. Happy coding!

