# localAssist

A self-hosted AI assistant that runs on your own machine. A chat UI in the
browser drives an agent that can actually do things — search the web, work with
your files, remember what matters, and more — with your data staying local by
default.

It's local-first: point it at a model running locally through Ollama and
nothing leaves your machine. Cloud models (Groq, OpenRouter) are supported too
if you'd rather trade privacy for speed, and you can switch between them per
conversation.

## What it can do

- **Chat with tools** — the agent decides when to reach for a tool and streams
  its answer back as it works.
- **Web search** — looks things up and pulls in current information.
- **Files** — reads, searches, and edits files in a sandboxed workspace. Edits
  are proposed for you to approve before anything is written.
- **Memory** — remembers facts about you across conversations.
- **Plans** — breaks bigger requests into steps and tracks its progress.
- **News & weather** — quick current info without leaving the chat.
- **Email drafts** — opens a prefilled compose window.
- **Skills** — slash commands (`/briefing`, `/research`, `/notes`) that chain a
  few steps into one action.
- **Voice input** — talk to it instead of typing.
- **Semantic search** — finds things by meaning, not just keywords, across your
  workspace.

## Notes

There's a built-in handwriting notes app — write on a canvas, and it runs OCR
on your pages in the background. The transcribed text gets indexed alongside
the rest of your workspace, so the assistant can search and reference your
handwritten notes right in chat.

## Local-first

The default posture is private: run a local model with Ollama and your prompts,
files, and memory never touch a third party. Web search and news reach out to
their APIs when you use those tools, but the model itself can stay entirely on
your machine. Cloud providers are there when you want them, not required.
