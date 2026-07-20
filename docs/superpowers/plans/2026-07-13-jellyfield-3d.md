# Jellyfield 3D Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. (This run executes via the Workflow orchestrator with the same task partition.)

**Goal:** Replace the 2D jellyfish background with a true-3D WebGL water column behind the same `window.Jellyfield` contract, with the shipped 2D engine retained as the automatic fallback.

**Architecture:** One static file `app/static/jellyfield.js` contains three IIFE-internal units — `Jellyfield2D` (the shipped engine, verbatim), `Jellyfield3D` (new WebGL renderer), and a selector that owns the public API and picks/demotes renderers at runtime. Templates swap their inlined engine for a `<script src>`; the CSP artifact preview inlines the same source.

**Tech Stack:** Raw WebGL (GLSL ES 1.00, WebGL2-first with WebGL1 + `ANGLE_instanced_arrays` fallback), Canvas 2D fallback, Flask static serving, headless Chrome + SwiftShader for verification. No external libraries.

## Global Constraints

- Public contract UNCHANGED: `window.Jellyfield = { mount(canvasEl), pulse(clientX, clientY, strength=1), setActivity(level) }`.
- The 2D engine source is at `/tmp/claude-1000/-home-kasm-user/34c46893-abe4-4301-999f-a8079a7429b5/scratchpad/webgl/jellyfield-2d-source.js` (26,595 chars, node --check OK) — include it VERBATIM except the minimal renames needed to nest it (see Task 1). Never rewrite its logic.
- Renderer selection in `mount()`: `prefers-reduced-motion: reduce` → 2D; else try `webgl2` → `webgl` (+`ANGLE_instanced_arrays`; missing → 2D); no context → 2D. Context lost → preventDefault + stop; restored → rebuild all GL resources; rebuild throws → demote to 2D for the session.
- Reduced-motion, pause-on-hidden, 150 ms debounced resize, DPR≤2, a11y column dim band (feather 120 px, floor 0.35), `pointer-events:none`/`aria-hidden` canvas: all preserved (spec "Invariants").
- No per-frame allocation in hot loops (preallocated typed arrays; uniforms set from scratch arrays).
- Templates: page-level scripts untouched; only the inline engine block is replaced by `<script src="{{ url_for('static', filename='jellyfield.js') }}"></script>`.
- All 110 pytest tests stay green. DO NOT COMMIT during execution; one commit at the end.
- Headless verification flags (proven in this environment): `google-chrome --headless=new --disable-gpu --use-angle=swiftshader --enable-unsafe-swiftshader` → `WebGL 2.0 (OpenGL ES 3.0 Chromium)`. Fallback proof: `--disable-webgl`.

---

## File Ownership (parallel-safe)

| Task | Owns |
|---|---|
| 1 Renderer | `app/static/jellyfield.js` (create), scratchpad demo/screenshots |
| 2 Templates | `app/templates/index.html`, `app/templates/login.html` |
| 3 Verify | any file (fix-forward) |
| 4 Preview | scratchpad `redesign/preview.html` |

### Task 1: `app/static/jellyfield.js` — 2D verbatim + 3D renderer + selector

**Interfaces — Produces:** the single public global `window.Jellyfield` (contract above). Internal shape (locked):

```js
(function () {
  'use strict';
  if (window.Jellyfield) { return; }

  function createJellyfield2D() { /* the extracted engine source, verbatim, minus its
    outer IIFE wrapper and its `window.Jellyfield = {...}` line — instead it ends with
    `return { mount: mount, pulse: pulse, setActivity: setActivity,
              detach: detach };` where detach() removes its window/document listeners
    (add detach; it is the ONLY addition to the 2D code) */ }

  function createJellyfield3D() { /* new WebGL renderer; returns
    { mount(canvas) -> boolean success, pulse, setActivity, detach } */ }

  /* selector: owns the public API, delegates to `active` renderer.
     mount(): reduced-motion ? use2D() : (try3D() || use2D()).
     Re-selects on prefers-reduced-motion change (detach old, mount new, same canvas).
     3D signals context-death via an onFatal callback -> selector demotes to 2D. */
  window.Jellyfield = { mount: ..., pulse: ..., setActivity: ... };
})();
```

- [ ] Step 1: Copy the extracted 2D source into `createJellyfield2D()` per the wrapper rules; `node --check`.
- [ ] Step 2: Build `Jellyfield3D`: bell mesh (≈24×14 parametric dome, instanced: native WebGL2 / ANGLE on WebGL1), vertex-shader contraction+wobble, fragment fresnel + medusa→brand translucent additive shading + emissive nucleus; tentacle ribbon strips (one instanced draw); fullscreen backdrop quad shader (gradient + animated light shafts); exponential fog to `#040A12`; camera idle sway + ±3° mouse parallax.
- [ ] Step 3: Interactions: pointer→view-ray flow field (push ⟂ ray, smoothstep radius, ≤1.5× brighten, ease-back); cursor-light billboard (2 s idle fade); `pulse()` shockwave shells at unprojected mid-depth (≤6 concurrent, oldest recycled; strength ≥2 → 3 shells @180 ms + origin flash); `setActivity` easing identical to 2D (0/0.25/1 semantics).
- [ ] Step 4: Invariants: column dim band via per-instance projected-x uniform pair (`u_dimL`,`u_dimR`, feather 120, floor 0.35; re-measured on resize); hidden-tab pause; debounced resize; DPR≤2; context-loss protocol per Global Constraints.
- [ ] Step 5: Demo page in scratchpad (`webgl/demo3d.html`) inlining the file; screenshot idle + bloom with the SwiftShader flags; VIEW both; iterate until the 3D jellyfish read as luminous creatures with real depth.
- [ ] Step 6: `node --check app/static/jellyfield.js` → OK. `python3 -m pytest app/tests -q` → 110 green (static file breaks nothing).

### Task 2: Template swap

- [ ] Step 1: In `index.html` and `login.html`, delete the entire inlined engine `<script>…</script>` block (the one assigning `window.Jellyfield = {`) and put in its place, same position (before the page script):

```html
<script src="{{ url_for('static', filename='jellyfield.js') }}"></script>
```

- [ ] Step 2: Touch NOTHING else. `grep -c "window.Jellyfield = {"` → 0 in both files; page scripts' optional-chained calls intact; `node --check` on remaining page scripts; Jinja tokens intact in login.
- [ ] Step 3: `python3 -m pytest app/tests -q` → 110 green (`test_template.py` greps still pass).

### Task 3: Verify (fix-forward allowed)

- [ ] Boot stub Jellyfin + real app; confirm `GET /static/jellyfield.js` → 200 with both engines present.
- [ ] SwiftShader screenshots, each VIEWED, none blank: `login-3d.png`, `index-3d-idle.png`, `index-3d-bloom.png` (capture ~300 ms after a scripted scan click), `index-3d-mobile.png` (390×844), `index-3d-reduced-motion.png` (must show the 2D static field — the selector routed to 2D).
- [ ] Fallback proof: same page with `--disable-webgl` → screenshot shows the animated-2D field, zero console errors.
- [ ] CDP console sweep on the authed page (SwiftShader run): zero errors/uncaught exceptions.
- [ ] `python3 -m pytest app/tests -q` → 110 green. No stray processes.

### Task 4: Artifact preview

- [ ] Replace the preview's inlined engine block with the full contents of `app/static/jellyfield.js`; keep everything else (mock API, demo cooldown) untouched; `node --check`; SwiftShader screenshot viewed. Republish is done by the orchestrator.

---

## Review dimensions (adversarial pass)

1. **GL correctness/perf:** shader compile/link error handling, instancing paths, per-frame allocation, draw-call count, blend-state resets, DPR resize, fog/depth math.
2. **Fallback & context loss:** every demotion trigger actually reaches a working 2D field on the SAME canvas (2D uses 2d context on a canvas that had a GL context — **a canvas can only ever have ONE context kind; `getContext('2d')` on a WebGL canvas returns null**. The selector MUST handle this: replace the canvas element with a fresh clone before mounting 2D. This is the #1 predicted bug.)
3. **Invariant regression:** reduced-motion both directions, dim band, hidden-tab, listeners detached on renderer swap (no double mousemove handlers), template contract greps.
4. **Visual:** does it actually look like The Deep in 3D — palette discipline, legibility over the dim band, no washed-out additive soup.

## Self-Review

Spec coverage: dual renderer + selector → T1; de-dup to static file → T1/T2; scene/interactions/invariants → T1 steps 2–4; verification incl. fallback proof + SwiftShader → T3; preview → T4; context-loss → T1 step 4 + review dim 2. Placeholders: none. Type consistency: `createJellyfield2D/3D` return `{mount, pulse, setActivity, detach}` everywhere; selector is the only owner of `window.Jellyfield`. Canvas single-context trap documented in review dim 2 and must be handled in T1's selector.
