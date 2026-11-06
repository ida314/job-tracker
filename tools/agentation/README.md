# Agentation overlay

Dev-only. Puts the [Agentation](https://www.agentation.com) annotation toolbar on the served pages,
so you can click an element, leave a note, and have a coding agent read it over MCP.

Agentation is a React component and this app has no React, so there are two small pieces:

- `entry.jsx` is the React layer. It mounts `<Agentation>` into its own root on the page.
- `overlay.py` is a local proxy that adds that bundle to each HTML response and widens the CSP
  for it (`script-src 'self'`, `connect-src` for the annotation server).

Nothing in `jobtracker/` knows about this, and neither image copies `tools/`.

## Run it

Once, and again after changing `entry.jsx` or a version:

```bash
npm --prefix tools/agentation ci && npm --prefix tools/agentation run build
```

Each time, start a preview (the `run-job-tracker` skill), then point the overlay at the URL it prints:

```bash
python3 tools/agentation/overlay.py --upstream http://127.0.0.1:8791
```

Open <http://127.0.0.1:8792>. The toolbar is the round icon at the bottom right.

Annotations reach the agent through the MCP server, which Claude Code starts itself:

```bash
claude mcp add agentation -- npx -y agentation-mcp@1.3.2 server
```

It listens on `:4747`. To use another port, change it there and in `overlay.py --endpoint`.

## Before you use it

- **Clicks are real.** The proxy forwards every request, so a button writes to whatever `--upstream`
  writes to. Use a preview, not a `serve` on your real files.
- **Annotations are stored in `~/.agentation/store.db`**, and can include text from the page you
  annotated. These pages can show your applications and mail.
