# Navi Trace Explorer

The trace explorer is Navi's read-oriented browser UI for trace events, loop
decisions, checkpoints, evaluations, and capability timing.

```bash
npm ci
npm run lint
npm run build
```

`npm run build` writes package assets to `src/navi/static/trace`. The FastAPI
application serves that packaged directory at `/ui/trace`, so the UI is present
in wheels, containers, and editable installs without depending on the repository
layout at runtime.

During development, run `npm run dev`; API requests continue to use the same
`/v1` paths as the packaged application.
