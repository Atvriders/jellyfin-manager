# Jellyfield 3D — Design

**Date:** 2026-07-13
**Status:** Approved (direction); spec pending owner review

## Problem

The owner wants the hero upgraded with a 3D WebGL element and chose the full option:
replace the entire 2D jellyfish background with a true 3D water column. The current
engine (`window.Jellyfield`, Canvas 2D) is freshly shipped, adversarially reviewed, and
inlined in three files. Replacing it wholesale is the riskiest interpretation, so this
design is built around never regressing what already works.

## Architecture: two renderers, one contract

```
window.Jellyfield = { mount(canvasEl), pulse(x, y, strength), setActivity(level) }
```

The public contract is UNCHANGED. Internally:

- **`Jellyfield3D`** (new): WebGL renderer. `mount()` tries `webgl2`, then `webgl`
  (shaders are GLSL ES 1.00 so one pair serves both).
- **`Jellyfield2D`** (existing engine, kept verbatim): the fallback. Engaged when no GL
  context is available, or when a lost GL context fails to restore.
- A thin selector owns `mount()`: it picks the renderer, and all three public methods
  delegate to whichever renderer is live. **A renderer swap can happen mid-session**
  (context loss → failed restore → 2D takes over on the same canvas element after the
  3D renderer detaches its listeners). Nobody ever sees a black background.

Context loss is a first-class path: `webglcontextlost` (preventDefault, stop loop),
`webglcontextrestored` (rebuild ALL GL resources: programs, buffers, textures), and a
restore that throws demotes to 2D permanently for the session.

### De-duplication (structural fix riding along)

The engine is currently inlined in `index.html`, `login.html`, and the artifact
preview — three divergent copies waiting to happen. It moves to
**`app/static/jellyfield.js`** (2D + 3D + selector, one IIFE), loaded via
`<script src="{{ url_for('static', filename='jellyfield.js') }}"></script>` before each
page's own script. Flask already serves `/static/` (the vendored cat proves it);
`COPY . .` in the Dockerfile ships it. Only the CSP-bound artifact preview keeps an
inlined copy of the same source.

## The 3D scene

Same art direction as shipped — abyssal blue-green water, medusa-violet creatures,
brand-cyan sonar — with real depth:

- **Jellyfish** (~50 desktop / ~24 mobile): one parametric bell mesh (≈24 segments ×
  14 rings), drawn **instanced** (native in WebGL2, `ANGLE_instanced_arrays` in WebGL1;
  if the extension is missing, fall back to the 2D renderer rather than N draw calls).
  Vertex shader does contraction squash/stretch by per-instance phase plus swim wobble;
  fragment shader does fresnel rim glow, translucent additive shading blending
  `--medusa` → `--brand` by depth, and a bright emissive nucleus. Contraction peak
  applies a forward velocity kick (in JS, per instance) — they visibly propel.
- **Tentacles**: 4–6 ribbon strips per jelly, one instanced draw for all; vertex shader
  sways them with phase-offset sinusoids, alpha fades toward the tips.
- **Volume**: jellies distributed through z ∈ [near, far]; exponential fog toward
  `--abyss`; jellies drift upward and respawn below, occasionally passing close to the
  camera. Camera has a gentle idle sway plus mouse parallax (±3° tilt toward pointer).
- **Backdrop**: one fullscreen quad shader renders the vertical water gradient and
  animated light shafts (replaces the prerendered 2D backdrop). The DOM `.vignette`
  overlay and all glass panels are untouched.

## Interactions (parity with 2D, now in 3D)

- **Cursor flow field**: the pointer unprojects to a view ray; jellies within a
  depth-scaled radius of the ray are pushed perpendicular to it (smoothstep falloff)
  and brighten to ~1.5×, easing back. A soft additive cursor-light billboard sits at a
  mid-depth plane, fading after 2 s idle or pointer exit.
- **Click** (`pulse` strength 1): an expanding shockwave shell (billboarded ring shader
  at the click's unprojected mid-depth position) pushes/brightens jellies as the
  wavefront passes their projected position. Max 6 concurrent, oldest recycled.
- **Scan** (`pulse` strength ≥ 2): three shells staggered ~180 ms + an origin flash +
  field-wide surge; `setActivity` keeps its 0..1 easing semantics (drift speed, pulse
  rate, glow, shimmer), including the 0.25 cooldown shimmer.

## Invariants carried over (non-negotiable)

- **Reduced motion**: defer entirely to the 2D renderer's existing static path — when
  the media query matches at mount (or flips to matching later), the selector uses the
  2D renderer, which already renders its reviewed static dim field with inert
  listeners. The 3D renderer never needs a static mode. Live changes honored in both
  directions (reduce→no-preference re-selects 3D).
- **A11y dim band**: jellies whose projected x falls within the measured `.column`
  band are dimmed (feather 120 px, floor 0.35) so text stays legible — computed
  per instance in the vertex stage or per fragment via `gl_FragCoord.x`, matching the
  2D behavior. Band re-measured on resize.
- **Housekeeping**: rAF paused on `document.hidden`; resize debounced 150 ms with
  early-return on unchanged size/DPR; DPR capped at 2; no per-frame allocation in the
  hot loop (preallocated typed arrays, no new objects/strings per frame);
  `pointer-events:none` + `aria-hidden` canvas; pages keep calling the engine only via
  optional chaining.
- **Templates change minimally**: the inline engine block is REPLACED by the static
  `<script src>` tag; page-level scripts (scan logic, dive log, XSS-safe rendering) are
  not touched. `test_template.py` greps must stay green.

## Verification

- All **110 pytest tests stay green** (backend untouched; template greps included).
- Headless Chrome **with SwiftShader** (`--use-angle=swiftshader` /
  `--enable-unsafe-swiftshader`) so WebGL truly renders: screenshots of idle, sonar
  bloom mid-burst, mobile 390×844, and reduced-motion. Each viewed, none blank.
- **Fallback proof**: the same page launched with `--disable-webgl` must still show
  the 2D jellyfish field (screenshot compared non-blank, `Jellyfield` still callable).
- **Console sweep** over CDP on the authed page: zero errors/uncaught exceptions.
- Artifact preview updated with the same 3D engine (inlined) and republished to the
  SAME URL.

## Out of scope

- Post-processing (bloom passes, DOF shaders), physics beyond the flow field,
  WebGPU, three.js or any external library, changes to panels/typography/copy,
  backend or API changes of any kind.
