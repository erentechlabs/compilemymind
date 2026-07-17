# 🧠 Compile My Mind

[![Hugo Version](https://img.shields.io/badge/Hugo-v0.164.0-8b5cf6?logo=hugo)](https://gohugo.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)

> **Deep dives into software engineering, networking, IT operations, cloud infrastructure, hardware, and the systems that power digital work.**

[**Visit the Website**](https://www.compilemymind.com/)

---

## 🎯 Overview

Welcome to the source code repository for **Compile My Mind**, an autonomous technical publishing and knowledge website.

This repository houses the Markdown content, Hugo templates, and configuration that generate the live website. It is built for fast performance and a clean reading experience, focusing on practical technical topics across software, infrastructure, operations, and systems.

### Key Topics Covered:
- **Cybersecurity and Identity** - Defensive controls, IAM, authentication, and Microsoft Entra ID.
- **Networking and IT Fundamentals** - Protocols, infrastructure, troubleshooting, and core concepts.
- **Azure and Cloud Certifications** - Microsoft cloud administration and version-aware exam guidance.
- **System Administration** - Windows, Linux, PowerShell, monitoring, backup, and operational practices.
- **Developer and IT Tools** - Git, GitHub, containers, automation, and infrastructure tooling.

---

## ✨ Features

- **Blazing Fast Performance:** Statically generated via Hugo for instant loading times.
- **Beautiful Typography & Design:** Uses the highly customizable *Mana* theme.
- **Syntax Highlighting:** Integrated code blocks powered by *Catppuccin* (Macchiato for dark mode, Frappe for light mode).
- **Responsive Layout:** Perfectly readable on mobile, tablet, and desktop.
- **Dark/Light Mode:** Seamless switching to match user preference.
- **SEO Optimized:** Automatic sitemap generation, structured data, and meta tags.

## Content pipeline

The site publishes technical guides through a small source-first Gemini pipeline. Each configured brief carries its own official documentation, so publication starts with a verified source set instead of a model-invented topic. The pipeline writes a post only after source availability, structure, length, citation, and Hugo build checks pass.

See [docs/CONTENT_PIPELINE.md](docs/CONTENT_PIPELINE.md) for setup, operations, and the editorial backlog.

---

## 🛠️ Tech Stack

*   **Framework:** [Hugo](https://gohugo.io/) v0.163.1 (Extended Edition)
*   **Theme:** [Mana Theme](https://github.com/Livour/hugo-mana-theme) (customized)
*   **Hosting/Deployment:** [Cloudflare Pages](https://pages.cloudflare.com/)
*   **Language:** Markdown / HTML / SCSS / Go Templates

---

## 📂 Project Structure

```text
compilemymind/
├── archetypes/      # Templates for new markdown files
├── assets/          # SCSS/CSS, JS, and global images/icons
├── content/         # The actual blog posts and pages (Markdown)
├── layouts/         # Custom HTML templates to override the theme
├── static/          # Static files served directly (e.g., robots.txt)
├── themes/          # The Mana theme module
└── hugo.toml        # Main Hugo configuration file
```

---

## Publisher

Articles use the site-level publisher identity **Compile My Mind**. The automated system does not create individual author profiles, personal biographies, or claims of manual review.

---

## 📜 License

The code and templates in this project are open-source and available under the [MIT License](LICENSE).
The website content remains subject to the repository and site licensing terms.
