# Frontend — Angular 21 Chat UI

Angular application for the Portfolio Chatbot. It talks to the FastAPI backend in the
repo root — this page covers running the frontend standalone against a running backend.

## Prerequisites

- Node.js 20+ and npm (the repo pins `npm@11.9.0`)
- The **backend running on `http://127.0.0.1:8000`** (see the [root README](../README.md))

The frontend calls the backend directly at `http://127.0.0.1:8000/api` (hardcoded in
`src/app/services/api/api.ts`); CORS on the backend already allows `localhost:4200`.

## Install

```bash
npm install
```

## Run (development)

```bash
npm start
```

Open [http://localhost:4200](http://localhost:4200). The app auto-reloads on source
changes. Start the backend **first**, or the app will load but API calls will fail.

**Recommended:** run backend in one terminal (`.\start.ps1` from the repo root) and the
frontend in another (`npm start` from this folder).

## Build (production)

```bash
npm run build
```

Output goes to `dist/`. The production build includes bundle budgets
(initial warning `500kB` / error `1MB`, per-component `8kB` / `20kB`).

To build with the development (unoptimized) configuration:

```bash
npm run build -- --configuration development
```

## Tests

```bash
npm test
```

Unit tests run with the Vitest runner (via `@angular/build:unit-test`).

## Formatting

Prettier is configured (`.prettierrc`):

```bash
npx prettier --write .
```

## Project structure

- `src/app/services/api/api.ts` — backend API client (base URL `http://127.0.0.1:8000/api`)
- `src/app/components/chat-interface/` — the chat UI
- `src/app/components/attached-documents/` — uploaded document list
- `public/` — static assets copied into the build

## npm scripts

| Script | Command | Description |
|---|---|---|
| `start` | `ng serve` | Dev server on `http://localhost:4200` |
| `build` | `ng build` | Production build to `dist/` |
| `watch` | `ng build --watch --configuration development` | Dev build (no serve) |
| `test` | `ng test` | Unit tests (Vitest) |